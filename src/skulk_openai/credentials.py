"""POSIX owner-only capability storage with generation-fenced token replacement.

This is protected local storage, not encryption or an OS sandbox. The operator
host and its user account remain trusted. No private SDK owner imports are used.
"""

import asyncio
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import final

from .auth_contracts import AuthFailure, Registration, TokenSet


@final
class CredentialStore:
    """Keep one installation's credentials beneath an owner-only directory."""

    def __init__(self, root: Path) -> None:
        """Create protected storage; refuse symlinks, foreign owners or loose modes."""
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._check(root, directory=True)
        self.root = root
        self.path = root / "credentials.sqlite3"
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            self._check(self.path)
        else:
            os.close(descriptor)
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS accounts (
                    registration_id TEXT PRIMARY KEY, subject TEXT NOT NULL,
                    client_id TEXT NOT NULL, generation INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, tokens TEXT
                );
            """)
            connection.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('host_id', ?)",
                (str(uuid.uuid4()),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('auth_epoch', '0')"
            )

    @staticmethod
    def _check(path: Path, *, directory: bool = False) -> None:
        value = path.lstat()
        correct_type = (
            stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
        )
        if (
            not correct_type
            or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) & 0o077
        ):
            raise AuthFailure("credentials_unavailable")

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection]:
        self._check(self.root, directory=True)
        self._check(self.path)
        connection = sqlite3.connect(self.path, timeout=3)
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA secure_delete=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def host_id(self) -> str:
        """Return the persistent opaque host identity across restart and logout."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='host_id'"
            ).fetchone()
            if row is None:
                raise AuthFailure("credentials_unavailable")
            return str(row[0])

    def epoch(self) -> int:
        """Return the durable fence for pending authorization attempts."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='auth_epoch'"
            ).fetchone()
            if row is None:
                raise AuthFailure("credentials_unavailable")
            return int(row[0])

    @staticmethod
    def registration_id(subject: str, client_id: str) -> str:
        """Bind identity and issued client without guessing a workspace from email."""
        return hashlib.sha256(json.dumps([subject, client_id]).encode()).hexdigest()

    @staticmethod
    def _tokens_json(tokens: TokenSet) -> str:
        value = {
            "access_token": tokens.access_token.get_secret_value(),
            "refresh_token": tokens.refresh_token.get_secret_value()
            if tokens.refresh_token
            else None,
            "id_token": tokens.id_token.get_secret_value(),
            "scopes": tokens.scopes,
            "expires_at": tokens.expires_at,
            "refresh_at": tokens.refresh_at,
        }
        return json.dumps(value, separators=(",", ":"), allow_nan=False)

    def load(self, registration_id: str | None = None) -> Registration | None:
        """Read the selected registration without writing or making network requests."""
        with self._connection() as connection:
            if registration_id is None:
                selected = connection.execute(
                    "SELECT value FROM metadata WHERE key='active'"
                ).fetchone()
                if selected is None:
                    return None
                registration_id = str(selected[0])
            row = connection.execute(
                "SELECT subject, client_id, generation, enabled, tokens "
                "FROM accounts WHERE registration_id=?",
                (registration_id,),
            ).fetchone()
            if row is None:
                return None
            return Registration(
                registration_id=registration_id,
                subject=str(row[0]),
                client_id=str(row[1]),
                generation=int(row[2]),
                enabled=bool(row[3]),
                tokens=TokenSet.model_validate_json(str(row[4]))
                if row[4] is not None
                else None,
            )

    def connect(
        self,
        subject: str,
        client_id: str,
        tokens: TokenSet,
        *,
        expected_generation: int | None,
        expected_epoch: int,
    ) -> Registration:
        """Commit verified credentials only if disconnect did not fence the attempt."""
        registration_id = self.registration_id(subject, client_id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT generation FROM accounts WHERE registration_id=?",
                (registration_id,),
            ).fetchone()
            epoch = connection.execute(
                "SELECT value FROM metadata WHERE key='auth_epoch'"
            ).fetchone()
            if epoch is None or int(epoch[0]) != expected_epoch:
                raise AuthFailure("stale_authorization")
            current = int(row[0]) if row is not None else None
            if current != expected_generation:
                raise AuthFailure("stale_authorization")
            count = int(
                connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            )
            if current is None and count >= 16:
                raise AuthFailure("credentials_unavailable")
            generation = (current or 0) + 1
            connection.execute(
                "INSERT OR REPLACE INTO accounts VALUES (?, ?, ?, ?, 1, ?)",
                (
                    registration_id,
                    subject,
                    client_id,
                    generation,
                    self._tokens_json(tokens),
                ),
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('active', ?)",
                (registration_id,),
            )
        result = self.load(registration_id)
        if result is None:
            raise AuthFailure("credentials_unavailable")
        return result

    def replace_tokens(self, registration: Registration, tokens: TokenSet) -> bool:
        """Atomically replace rotating credentials, refusing a concurrent disconnect."""
        with self._connection() as connection:
            result = connection.execute(
                "UPDATE accounts SET tokens=? WHERE registration_id=? "
                "AND generation=? AND enabled=1",
                (
                    self._tokens_json(tokens),
                    registration.registration_id,
                    registration.generation,
                ),
            )
            return result.rowcount == 1

    def disconnect(
        self,
        registration_id: str | None = None,
        *,
        expected_generation: int | None = None,
    ) -> Registration | None:
        """Fence admission and rotation, retaining only registration metadata."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if registration_id is None:
                active = connection.execute(
                    "SELECT value FROM metadata WHERE key='active'"
                ).fetchone()
                if active is None:
                    connection.execute(
                        "UPDATE metadata SET value=CAST(value AS INTEGER)+1 "
                        "WHERE key='auth_epoch'"
                    )
                    return None
                registration_id = str(active[0])
            row = connection.execute(
                "SELECT subject, client_id, generation, enabled, tokens "
                "FROM accounts WHERE registration_id=?",
                (registration_id,),
            ).fetchone()
            if row is None:
                return None
            if expected_generation is not None and int(row[2]) != expected_generation:
                return None
            connection.execute(
                "UPDATE metadata SET value=CAST(value AS INTEGER)+1 "
                "WHERE key='auth_epoch'"
            )
            previous = Registration(
                registration_id=registration_id,
                subject=str(row[0]),
                client_id=str(row[1]),
                generation=int(row[2]),
                enabled=bool(row[3]),
                tokens=TokenSet.model_validate_json(str(row[4]))
                if row[4] is not None
                else None,
            )
            connection.execute(
                "UPDATE accounts SET tokens=NULL, enabled=0, generation=generation+1 "
                "WHERE registration_id=?",
                (registration_id,),
            )
            return previous

    @asynccontextmanager
    async def session_lock(self, registration_id: str) -> AsyncGenerator[None]:
        """Serialize refresh across processes without a SQL lock over HTTP."""
        if len(registration_id) != 64 or any(
            c not in "0123456789abcdef" for c in registration_id
        ):
            raise AuthFailure("credentials_unavailable")
        descriptor = os.open(
            self.root / f"{registration_id}.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        deadline = time.monotonic() + 12
        try:
            self._check(self.root / f"{registration_id}.lock")
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AuthFailure("credentials_unavailable") from None
                    await asyncio.sleep(0.01)
            yield
        finally:
            os.close(descriptor)
