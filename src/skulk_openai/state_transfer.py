"""Offline protected state custody for explicit native adoption.

Snapshots contain provider secrets and encrypted windows. They stay beneath
owner-only directories, are never included in releases, and never authorize a
request. Rollback keeps the current journal; it never restores an old nonce/CAS
history. A disaster restore creates a new quarantined store instead of rewinding
an active installation.
"""

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import stat
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Literal, final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import Field, TypeAdapter

from skulk_inference_reference.contracts import FrozenContract, Identifier

from .auth_contracts import AuthFailure
from .checkpoints import (
    MAX_RECORD_BYTES,
    CheckpointStore,
    ProtectedRecord,
    RecordMetadata,
)
from .credentials import CredentialStore
from .settings import IntegrationSettings

_TABLES = frozenset(
    {
        "metadata",
        "accounts",
        "grants",
        "gateway_budget",
        "grant_nonces",
        "gateway_nonces",
        "continuation_callers",
        "continuation_records",
        "continuation_nonces",
    }
)
type Scalar = str | bytes | int | float | None
_ROW = TypeAdapter(tuple[Scalar, ...])
_TEXT = TypeAdapter(str)
_MAX_DATABASE_BYTES = 4 * 1024**3


@final
class StateSnapshot(FrozenContract):
    """Integrity receipt for one coherent private database/key snapshot."""

    format: Literal[1] = 1
    database_sha256: str = Field(
        pattern=r"^[a-f0-9]{64}$", description="Exact snapshot database bytes."
    )
    key_sha256: str | None = Field(
        pattern=r"^[a-f0-9]{64}$",
        description="Matching protected window key, or absent before checkpoint use.",
    )
    state_digest: str = Field(
        pattern=r"^[a-f0-9]{64}$",
        description="Versioned logical schema/row digest for stale-snapshot refusal.",
    )


@final
class AdoptionBinding(FrozenContract):
    """Explicit independent identities; neither revision is a conversion."""

    legacy_id: Identifier
    version: str = Field(min_length=1, max_length=64)
    legacy_revision: str = Field(pattern=r"^[a-f0-9]{16}$")
    native_id: Identifier
    native_revision: str = Field(pattern=r"^[a-f0-9]{64}$")


@final
class NativeAdoption(FrozenContract):
    """Store-local migration receipt matched against actual native startup."""

    format: Literal[1] = 1
    store_host_id: Identifier = Field(
        description="Unchanged capability credential-store identity."
    )
    legacy_target_node: Identifier = Field(
        description="Original application proof target; not a native identity alias."
    )
    native_node_id: Identifier = Field(
        description="Actual installed child identity supplied by its owner."
    )
    native_bundle_id: Identifier = Field(
        description="Explicit signed native application bundle."
    )
    bindings: tuple[AdoptionBinding, ...] = Field(min_length=1, max_length=16)
    snapshot_state_digest: str = Field(
        pattern=r"^[a-f0-9]{64}$",
        description="State digest at initial enrollment; never a caller grant.",
    )


def _private_read(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        value = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != os.geteuid()
            or value.st_mode & 0o077
            or value.st_size > maximum
        ):
            raise AuthFailure("credentials_unavailable")
        data = source.read(maximum + 1)
    if len(data) > maximum:
        raise AuthFailure("credentials_unavailable")
    return data


def _private_write(path: Path, data: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def application_fence(root: Path) -> Generator[None]:
    """Hold one stable inode throughout serving or migration; never unlink it."""
    _protected_directory(root)
    with _lock(root / "application-worker.lock"):
        yield


@contextmanager
def _lock(path: Path) -> Generator[None]:
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise AuthFailure("credentials_unavailable")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AuthFailure("credentials_unavailable") from None
        yield
    finally:
        os.close(descriptor)


def _protected_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise AuthFailure("credentials_unavailable")


@contextmanager
def _offline(store: CredentialStore) -> Generator[sqlite3.Connection]:
    # Older checkpoint-enabled children use the second stable fence. The owner
    # must stop pre-adoption children before invoking any offline operation.
    with (
        application_fence(store.root),
        _lock(store.root / "continuation-worker.lock"),
        store.transaction() as connection,
    ):
        connection.execute("BEGIN IMMEDIATE")
        with ExitStack() as locks:
            for raw in connection.execute(
                "SELECT registration_id FROM accounts ORDER BY registration_id"
            ):
                identifier = _TEXT.validate_python(
                    _ROW.validate_python(raw, strict=True)[0], strict=True
                )
                if len(identifier) != 64 or any(
                    c not in "0123456789abcdef" for c in identifier
                ):
                    raise AuthFailure("credentials_unavailable")
                locks.enter_context(_lock(store.root / (identifier + ".lock")))
            yield connection


def _digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256(b"skulk-openai-state-v1\0")

    def update(row: tuple[Scalar, ...]) -> None:
        for value in row:
            if value is None:
                data, kind = b"", b"n"
            elif isinstance(value, bytes):
                data, kind = value, b"b"
            elif isinstance(value, str):
                data, kind = value.encode(), b"s"
            elif isinstance(value, int):
                data, kind = str(value).encode(), b"i"
            else:
                if not math.isfinite(value):
                    raise AuthFailure("credentials_unavailable")
                data, kind = value.hex().encode(), b"f"
            digest.update(kind + len(data).to_bytes(8, "big") + data)
        digest.update(b"r")

    tables: list[str] = []
    for raw in connection.execute(
        "SELECT type,name,sql FROM sqlite_schema "
        "WHERE name NOT GLOB 'sqlite_*' ORDER BY name"
    ):
        row = _ROW.validate_python(raw, strict=True)
        update(row)
        if row[0] != "table":
            raise AuthFailure("credentials_unavailable")
        name = _TEXT.validate_python(row[1], strict=True)
        if name not in _TABLES:
            raise AuthFailure("credentials_unavailable")
        tables.append(name)
    for name in sorted(tables):
        update((name,))
        for raw in connection.execute('SELECT * FROM "' + name + '" ORDER BY rowid'):
            update(_ROW.validate_python(raw, strict=True))
    return digest.hexdigest()


def _key(root: Path) -> bytes | None:
    path = root / "continuation.key"
    try:
        raw = _private_read(path, 32)
    except FileNotFoundError:
        return None
    if len(raw) != 32:
        raise AuthFailure("credentials_unavailable")
    return raw


def _verify_windows(
    connection: sqlite3.Connection,
    key: bytes | None,
    adoption: NativeAdoption | None = None,
) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' "
            "AND name='continuation_records'"
        ).fetchone()
        is None
    ):
        return
    for raw in connection.execute(
        "SELECT scope,caller_id,version,phase,expires,payload,allocation_bytes,"
        "handle_digest FROM continuation_records"
    ):
        row = _ROW.validate_python(raw, strict=True)
        scope, caller, version, phase, expires, payload, allocation, handle = row
        if (
            key is None
            or not isinstance(payload, bytes)
            or not 32 <= len(payload) <= MAX_RECORD_BYTES
        ):
            raise AuthFailure("credentials_unavailable")
        aad = json.dumps(
            [
                "checkpoint-v1",
                scope,
                caller,
                version,
                phase,
                float(expires) if isinstance(expires, (int, float)) else None,
                allocation,
            ],
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        try:
            decoded = AESGCM(key).decrypt(payload[:12], payload[12:], aad)
        except InvalidTag:
            raise AuthFailure("credentials_unavailable") from None
        size = int.from_bytes(decoded[:4], "big")
        if not 1 <= size <= 65536 or size + 4 > len(decoded):
            raise AuthFailure("credentials_unavailable")
        metadata = RecordMetadata.model_validate_json(decoded[4 : 4 + size])
        state = metadata.state
        if adoption is not None:
            target = state.binding.target
            if target.target_node != adoption.legacy_target_node or not any(
                binding.legacy_id == target.callable_id
                and binding.legacy_revision == target.descriptor_revision
                and binding.version == "0.1.0"
                for binding in adoption.bindings
            ):
                # Existing encrypted histories cannot be silently rebound to a
                # different logical target, profile or legacy callable family.
                raise AuthFailure("credentials_unavailable")
        if (
            (state.version, state.phase, state.expires_at) != (version, phase, expires)
            or CheckpointStore.allocation(
                ProtectedRecord(metadata, decoded[4 + size :])
            )
            != allocation
            or (
                hashlib.sha256(state.handle.encode()).hexdigest()
                if state.handle
                else None
            )
            != handle
        ):
            raise AuthFailure("credentials_unavailable")


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_size > _MAX_DATABASE_BYTES
        ):
            raise AuthFailure("credentials_unavailable")
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def open_existing_state(root: Path) -> CredentialStore:
    """Refuse absent or foreign journals before any constructor can initialize them."""
    _protected_directory(root)
    path = root / "credentials.sqlite3"
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise AuthFailure("credentials_unavailable")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3) as connection:
        for table, expected in (
            ("metadata", ("key", "value")),
            (
                "accounts",
                (
                    "registration_id",
                    "subject",
                    "client_id",
                    "generation",
                    "enabled",
                    "tokens",
                ),
            ),
        ):
            columns = tuple(
                str(row[1])
                for row in connection.execute('PRAGMA table_info("' + table + '")')
            )
            if columns != expected:
                raise AuthFailure("credentials_unavailable")
        if connection.execute(
            "SELECT COUNT(*) FROM metadata WHERE key IN ('host_id','auth_epoch')"
        ).fetchone() != (2,):
            raise AuthFailure("credentials_unavailable")
    return CredentialStore(root)


def snapshot_state(store: CredentialStore, destination: Path) -> StateSnapshot:
    """Back up a stopped store/key coherently without mutating account history."""
    destination.mkdir(mode=0o700)
    _protected_directory(destination)
    with _offline(store) as connection:
        key = _key(store.root)
        _verify_windows(connection, key)
        state_digest = _digest(connection)
        path = destination / "credentials.sqlite3"
        _private_write(path, b"")
        with sqlite3.connect(
            store.path.as_uri() + "?mode=ro", uri=True, timeout=3
        ) as source:
            target = sqlite3.connect(path)
            try:
                source.backup(target)
                if _digest(target) != state_digest:
                    raise AuthFailure("credentials_unavailable")
            finally:
                target.close()
        if key is not None:
            _private_write(destination / "continuation.key", key)
        result = StateSnapshot(
            database_sha256=_file_digest(path),
            key_sha256=hashlib.sha256(key).hexdigest() if key is not None else None,
            state_digest=state_digest,
        )
        _private_write(destination / "snapshot.json", result.model_dump_json().encode())
    return result


def verify_snapshot(root: Path) -> StateSnapshot:
    """Verify coherent files and encrypted records before adoption or restore."""
    _protected_directory(root)
    receipt = StateSnapshot.model_validate_json(
        _private_read(root / "snapshot.json", 4096)
    )
    key = _key(root)
    if (
        _file_digest(root / "credentials.sqlite3") != receipt.database_sha256
        or (hashlib.sha256(key).hexdigest() if key is not None else None)
        != receipt.key_sha256
    ):
        raise AuthFailure("credentials_unavailable")
    with sqlite3.connect(
        (root / "credentials.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=3
    ) as connection:
        if _digest(connection) != receipt.state_digest:
            raise AuthFailure("credentials_unavailable")
        _verify_windows(connection, key)
    return receipt


def enroll_native(
    store: CredentialStore, backup: Path, adoption: NativeAdoption
) -> None:
    """Enroll native state only against its current coherent snapshot."""
    snapshot = verify_snapshot(backup)
    with _offline(store) as connection:
        key = _key(store.root)
        host = connection.execute(
            "SELECT value FROM metadata WHERE key='host_id'"
        ).fetchone()
        if (
            host is None
            or str(host[0]) != adoption.store_host_id
            or adoption.snapshot_state_digest != snapshot.state_digest
            or _digest(connection) != snapshot.state_digest
            or (hashlib.sha256(key).hexdigest() if key is not None else None)
            != snapshot.key_sha256
        ):
            raise AuthFailure("credentials_unavailable")
        _verify_windows(connection, key, adoption)
        _private_write(
            store.root / "native-adoption.json", adoption.model_dump_json().encode()
        )


def read_native_adoption(root: Path) -> NativeAdoption:
    """Read a private receipt and match its original store identity without writes."""
    _protected_directory(root)
    result = NativeAdoption.model_validate_json(
        _private_read(root / "native-adoption.json", 32768)
    )
    path = root / "credentials.sqlite3"
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise AuthFailure("credentials_unavailable")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3) as connection:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='host_id'"
        ).fetchone()
        if row is None or str(row[0]) != result.store_host_id:
            raise AuthFailure("credentials_unavailable")
    return result


def _quarantine(database: Path) -> None:
    connection = sqlite3.connect(database, timeout=3)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                )
            }
            connection.execute(
                "UPDATE metadata SET value=CAST(value AS INTEGER)+1 "
                "WHERE key='auth_epoch'"
            )
            connection.execute("UPDATE accounts SET generation=generation+1")
            if "grants" in tables:
                connection.execute("UPDATE grants SET enabled=0,remaining=0")
            if "continuation_callers" in tables:
                connection.execute(
                    "UPDATE continuation_callers SET enabled=0,generation=generation+1"
                )
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='integration_settings'"
            ).fetchone()
            settings = (
                IntegrationSettings()
                if row is None
                else IntegrationSettings.model_validate_json(str(row[0]))
            )
            settings = settings.model_copy(
                update={"inference_enabled": False, "gateway_public_key": None}
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key,value) "
                "VALUES ('integration_settings',?)",
                (settings.model_dump_json(),),
            )
    finally:
        connection.close()


def restore_quarantined(backup: Path, destination: Path) -> CredentialStore:
    """Restore a new private root with old caller authority disabled.

    Existing roots are never replaced. Keep nonces, budgets, CAS versions and
    ciphertext. Fence pending sign-ins/account generations and revoke caller
    grants and gateway enrollment. An interrupted restore stays unservable.
    """
    snapshot = verify_snapshot(backup)
    destination.mkdir(mode=0o700)
    _protected_directory(destination)
    pending = destination / "state-transfer.pending"
    _private_write(pending, b"quarantine required")
    with (
        application_fence(destination),
        _lock(destination / "continuation-worker.lock"),
    ):
        # Older runtimes do not know the pending marker. Do not expose their
        # recognized journal name until restored authority is quarantined.
        target = destination / "state-transfer.sqlite3"
        _private_write(target, b"")
        with sqlite3.connect(
            (backup / "credentials.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=3
        ) as source:
            connection = sqlite3.connect(target)
            try:
                source.backup(connection)
            finally:
                connection.close()
        key = _key(backup)
        if key is not None:
            _private_write(destination / "continuation.key", key)
        # Verify the actual copied state before releasing quarantine. The
        # source may have changed since its initial integrity check.
        if (hashlib.sha256(key).hexdigest() if key is not None else None) != (
            snapshot.key_sha256
        ):
            raise AuthFailure("credentials_unavailable")
        with sqlite3.connect(target) as copied:
            if _digest(copied) != snapshot.state_digest:
                raise AuthFailure("credentials_unavailable")
            _verify_windows(copied, key)
        _quarantine(target)
        # Hard-link publication is atomic and refuses an existing destination;
        # even a concurrent old runtime's newly initialized store is preserved.
        os.link(target, destination / "credentials.sqlite3", follow_symlinks=False)
        target.unlink()
        pending.unlink()
        directory = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return CredentialStore(destination)
