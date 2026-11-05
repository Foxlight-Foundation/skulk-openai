"""Capability-local caller identity and encrypted transactional checkpoint custody.

No HTTP endpoint or SDK descriptor advertises this implementation yet. Trusted
owner code issues/revokes caller keys; invocation code must use the worker lease.
Provider windows never enter settings, discovery, replicated state or diagnostics.
"""

import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import final

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import Field, JsonValue, SecretStr

from skulk_inference_reference.continuation import (
    CallerScope,
    ContinuationFailure,
    ContinuationReceipt,
    ContinuationState,
    ContinuationTarget,
    OwnerContext,
    VerifiedCaller,
    authorize_receipt,
    complete,
    dispatch,
    interrupt,
    reserve,
)
from skulk_inference_reference.continuation_auth import (
    ContinuationCredential,
    ContinuationProof,
    ReceiptProof,
    canonical_proof,
)
from skulk_inference_reference.contracts import FrozenContract

from .admission import GrantStore
from .auth_contracts import AuthFailure
from .credentials import CredentialStore
from .provider import CatalogSnapshot
from .settings import IntegrationSettings, account_binding

MAX_WINDOW_BYTES = 16 * 1024 * 1024
MAX_RECORD_BYTES = MAX_WINDOW_BYTES + 65_536 + 32


@final
class CheckpointPolicy(FrozenContract):
    """Explicit owner retention and sealed-payload bounds, separate from usage."""

    retention_seconds: int = Field(ge=1, le=86400)
    maximum_records: int = Field(ge=1, le=4096)
    maximum_storage_bytes: int = Field(ge=1, le=2**31)


@final
class RecordMetadata(FrozenContract):
    """Private authenticated metadata; serialization never includes window bytes."""

    state: ContinuationState
    receipt: ContinuationReceipt | None = None


@final
@dataclass(frozen=True)
class ProtectedRecord:
    """Process-local decoded record, never a control-plane model or log value."""

    metadata: RecordMetadata
    window: bytes = field(repr=False)


@final
@dataclass(frozen=True)
class Authorization:
    """Fresh facts resolved inside the same transaction as each state mutation."""

    caller: VerifiedCaller
    owner: OwnerContext
    settings: IntegrationSettings


def _scope(caller: CallerScope, conversation_id: str) -> str:
    return hashlib.sha256(
        json.dumps([caller.authority, caller.subject, conversation_id]).encode()
    ).hexdigest()


class CheckpointStore:
    """Issue distinct caller keys and own encrypted records in the account journal.

    Only a trusted local owner may call issue/revoke. Signing material is returned
    once; only its public key is persisted. Storage/retention bounds are explicit
    constructor arguments, not upstream allowance or inference output limits.
    """

    def __init__(
        self,
        credentials: CredentialStore,
        *,
        target_node: str,
        callable_id: str,
        descriptor_revision: str,
        clock: Callable[[], float],
        retention_seconds: int,
        maximum_records: int,
        maximum_storage_bytes: int,
    ) -> None:
        """Create tables/key only in the supplied protected capability state root."""
        policy = CheckpointPolicy(
            retention_seconds=retention_seconds,
            maximum_records=maximum_records,
            maximum_storage_bytes=maximum_storage_bytes,
        )
        self.credentials = credentials
        self.target = ContinuationTarget(
            target_node=target_node,
            callable_id=callable_id,
            descriptor_revision=descriptor_revision,
            model="unselected",
        )
        self.clock = clock
        self.retention_seconds = policy.retention_seconds
        self.maximum_records = policy.maximum_records
        self.maximum_storage_bytes = policy.maximum_storage_bytes
        self.authority = (
            "capability." + hashlib.sha256(credentials.host_id().encode()).hexdigest()
        )
        with credentials.transaction() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS continuation_callers (
                    id TEXT PRIMARY KEY, public_key TEXT UNIQUE NOT NULL,
                    registration_id TEXT NOT NULL, account_generation INTEGER NOT NULL,
                    models TEXT NOT NULL, generation INTEGER NOT NULL,
                    enabled INTEGER NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS continuation_records (
                    scope TEXT PRIMARY KEY, caller_id TEXT NOT NULL,
                    version INTEGER NOT NULL, phase TEXT NOT NULL,
                    expires REAL NOT NULL, payload BLOB NOT NULL,
                    handle_digest TEXT UNIQUE, allocation_bytes INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS continuation_nonces (
                    caller_id TEXT NOT NULL, nonce TEXT NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY (caller_id, nonce)
                );
                CREATE TABLE IF NOT EXISTS gateway_budget (
                    hour INTEGER PRIMARY KEY, attempts INTEGER NOT NULL
                );
            """)
        self._cipher = AESGCM(self._key())

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        """Provide a protected transaction with content-free storage failures."""
        try:
            with self.credentials.transaction() as connection:
                yield connection
        except (AuthFailure, OSError, sqlite3.Error, ValueError):
            raise ContinuationFailure("storage_unavailable") from None

    def _key(self) -> bytes:
        path = self.credentials.root / "continuation.key"
        if not path.exists():
            # Losing the key must not create an empty/new history beside an
            # existing encrypted journal. Restore the matching key or purge by
            # an explicit owner action; never silently downgrade to plaintext.
            with self.transaction() as connection:
                if connection.execute(
                    "SELECT 1 FROM continuation_records LIMIT 1"
                ).fetchone():
                    raise ContinuationFailure("storage_unavailable")
            temporary = self.credentials.root / (
                ".continuation-" + secrets.token_hex(16)
            )
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as target:
                    target.write(AESGCM.generate_key(bit_length=256))
                    target.flush()
                    os.fsync(target.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
                else:
                    parent = os.open(self.credentials.root, os.O_RDONLY)
                    try:
                        os.fsync(parent)
                    finally:
                        os.close(parent)
            finally:
                temporary.unlink(missing_ok=True)
        self.credentials.check_protected_path(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            raw = source.read(33)
        if len(raw) != 32:
            raise ContinuationFailure("storage_unavailable")
        return raw

    def _owner(
        self, connection: sqlite3.Connection, target: ContinuationTarget
    ) -> tuple[OwnerContext, IntegrationSettings, CatalogSnapshot]:
        if target.model_copy(update={"model": "unselected"}) != self.target:
            raise ContinuationFailure("scope_mismatch")
        settings_row = connection.execute(
            "SELECT value FROM metadata WHERE key='integration_settings'"
        ).fetchone()
        catalog_row = connection.execute(
            "SELECT value FROM metadata WHERE key='catalog'"
        ).fetchone()
        if (
            settings_row is None
            or catalog_row is None
            or len(str(catalog_row[0])) > 65536
        ):
            raise ContinuationFailure("authority_unavailable")
        settings = IntegrationSettings.model_validate_json(str(settings_row[0]))
        catalog = CatalogSnapshot.model_validate_json(str(catalog_row[0]))
        try:
            GrantStore.require_active_account(
                connection, catalog.registration_id, catalog.generation
            )
        except AuthFailure:
            raise ContinuationFailure("authority_unavailable") from None
        if (
            not settings.inference_enabled
            or settings.processing != "allow_openai"
            or settings.usage != "app_settings"
            or settings.account_binding
            != account_binding(catalog.registration_id, catalog.generation)
            or target.model not in settings.models
            or target.model not in {model.slug for model in catalog.models}
            or catalog.expires_at <= self.clock()
        ):
            raise ContinuationFailure("authority_unavailable")
        owner = OwnerContext(
            target=target,
            account_epoch=account_binding(catalog.registration_id, catalog.generation),
            policy_epoch=hashlib.sha256(
                (
                    settings.model_dump_json()
                    + catalog.model_dump_json(exclude={"expires_at"})
                ).encode()
            ).hexdigest(),
            enabled=True,
            expires_at=catalog.expires_at,
        )
        return owner, settings, catalog

    def issue(
        self, *, models: tuple[str, ...], expires_at: float
    ) -> ContinuationCredential:
        """Owner-issue a client key with explicit models/expiry and no usage ceiling."""
        # Strict model construction rejects nonfinite expiry before writing any
        # delegation. This Python owner API is not exposed to inference input.
        checked = VerifiedCaller(
            CallerScope(authority=self.authority, subject="pending", generation=1),
            expires_at=expires_at,
        )
        if not models or len(models) > 128 or len(set(models)) != len(models):
            raise ContinuationFailure("authority_unavailable")
        key = Ed25519PrivateKey.generate()
        caller_id = secrets.token_hex(16)
        with self.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self.clock()
            if checked.expires_at <= now:
                raise ContinuationFailure("expired")
            catalog: CatalogSnapshot | None = None
            for model in models:
                _, _, catalog = self._owner(
                    connection, self.target.model_copy(update={"model": model})
                )
            assert catalog is not None
            self.prune(connection)
            if (
                connection.execute(
                    "SELECT COUNT(*) FROM continuation_callers"
                ).fetchone()[0]
                >= 128
            ):
                raise ContinuationFailure("capacity_exceeded")
            connection.execute(
                "INSERT INTO continuation_callers VALUES (?,?,?,?,?,1,1,?)",
                (
                    caller_id,
                    key.public_key().public_bytes_raw().hex(),
                    catalog.registration_id,
                    catalog.generation,
                    json.dumps(models),
                    expires_at,
                ),
            )
        return ContinuationCredential(
            caller_id=caller_id, private_key=SecretStr(key.private_bytes_raw().hex())
        )

    def revoke(self, caller_id: str) -> None:
        """Owner-revoke admission and remove that client's checkpoints atomically."""
        with self.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE continuation_callers SET enabled=0,generation=generation+1 "
                "WHERE id=?",
                (caller_id,),
            )
            connection.execute(
                "DELETE FROM continuation_records WHERE caller_id=?", (caller_id,)
            )

    def authenticate(
        self,
        connection: sqlite3.Connection,
        proof: ContinuationProof | ReceiptProof,
        *,
        admitting: bool = True,
    ) -> Authorization:
        """Resolve current caller/account/policy inside the owner's transaction."""
        row = connection.execute(
            "SELECT public_key,registration_id,account_generation,models,generation,"
            "enabled,expires FROM continuation_callers WHERE id=?",
            (proof.caller_id,),
        ).fetchone()
        now = self.clock()
        if row is None or not row[5] or float(row[6]) <= now:
            raise ContinuationFailure("authority_unavailable")
        if admitting and not now < proof.request.expires_at <= now + 60:
            raise ContinuationFailure("expired")
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(str(row[0]))).verify(
                bytes.fromhex(proof.signature),
                canonical_proof(proof.caller_id, proof.request),
            )
        except (InvalidSignature, ValueError):
            raise ContinuationFailure("authority_unavailable") from None
        owner, settings, catalog = self._owner(connection, proof.request.target)
        if (
            catalog.registration_id != str(row[1])
            or catalog.generation != int(row[2])
            or proof.request.target.model not in json.loads(str(row[3]))
        ):
            raise ContinuationFailure("scope_mismatch")
        return Authorization(
            VerifiedCaller(
                CallerScope(
                    authority=self.authority,
                    subject=proof.caller_id,
                    generation=int(row[4]),
                ),
                expires_at=float(row[6]),
            ),
            owner,
            settings,
        )

    def prune(self, connection: sqlite3.Connection) -> None:
        """Remove expired private records and credentials in a write transaction."""
        now = self.clock()
        connection.execute("DELETE FROM continuation_records WHERE expires<=?", (now,))
        connection.execute("DELETE FROM continuation_nonces WHERE expires<=?", (now,))
        connection.execute(
            "DELETE FROM continuation_nonces WHERE caller_id IN "
            "(SELECT id FROM continuation_callers WHERE expires<=? OR enabled=0)",
            (now,),
        )
        connection.execute(
            "DELETE FROM continuation_records WHERE caller_id IN "
            "(SELECT id FROM continuation_callers WHERE expires<=? OR enabled=0)",
            (now,),
        )
        connection.execute(
            "DELETE FROM continuation_callers WHERE expires<=? OR enabled=0", (now,)
        )

    def fence(
        self, connection: sqlite3.Connection, caller_id: str, nonce: str, expires: float
    ) -> None:
        """Spend an attempt/query nonce until its caller credential expires."""
        if (
            connection.execute("SELECT COUNT(*) FROM continuation_nonces").fetchone()[0]
            >= 1_000_000
        ):
            raise ContinuationFailure("capacity_exceeded")
        try:
            connection.execute(
                "INSERT INTO continuation_nonces VALUES (?,?,?)",
                (caller_id, nonce, expires),
            )
        except sqlite3.IntegrityError:
            raise ContinuationFailure("handle_unavailable") from None

    @staticmethod
    def _aad(
        scope: str, caller_id: str, state: ContinuationState, allocation: int
    ) -> bytes:
        return json.dumps(
            [
                "checkpoint-v1",
                scope,
                caller_id,
                state.version,
                state.phase,
                state.expires_at,
                allocation,
            ],
            separators=(",", ":"),
            allow_nan=False,
        ).encode()

    @staticmethod
    def allocation(record: ProtectedRecord) -> int:
        """Charge enough sealed bytes to persist the worst cleanup transition.

        Pending state may grow when its phase/version changes at interruption.
        Reserve that exact serialized growth before admission so neither normal
        cleanup nor crash recovery needs space beyond the admitted allocation.
        The 32 bytes cover metadata framing, the GCM nonce and authentication tag.
        """
        metadata = record.metadata
        size = len(metadata.model_dump_json().encode())
        state = metadata.state
        if state.phase in {"reserved", "dispatched"}:
            dispatched = (
                replace(state, phase="dispatched", version=state.version + 1)
                if state.phase == "reserved"
                else state
            )
            interrupted = interrupt(dispatched)
            assert interrupted is not None
            cleanup = RecordMetadata(state=interrupted, receipt=metadata.receipt)
            size = max(size, len(cleanup.model_dump_json().encode()))
        return 32 + size + len(record.window)

    def load_record(
        self, connection: sqlite3.Connection, scope: str
    ) -> ProtectedRecord | None:
        """Read and authenticate one private record in the existing transaction."""
        row = connection.execute(
            "SELECT caller_id,version,phase,expires,length(payload),handle_digest,"
            "allocation_bytes "
            "FROM continuation_records WHERE scope=?",
            (scope,),
        ).fetchone()
        if row is None:
            return None
        if not 32 <= int(row[4]) <= MAX_RECORD_BYTES:
            raise ContinuationFailure("storage_unavailable")
        payload = bytes(
            connection.execute(
                "SELECT payload FROM continuation_records WHERE scope=?", (scope,)
            ).fetchone()[0]
        )
        aad = json.dumps(
            [
                "checkpoint-v1",
                scope,
                str(row[0]),
                int(row[1]),
                str(row[2]),
                float(row[3]),
                int(row[6]),
            ],
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        try:
            raw = self._cipher.decrypt(payload[:12], payload[12:], aad)
            size = int.from_bytes(raw[:4], "big")
            if not 1 <= size <= 65536 or size + 4 > len(raw):
                raise ValueError("invalid metadata")
            metadata = RecordMetadata.model_validate_json(raw[4 : 4 + size])
            if self._aad(scope, str(row[0]), metadata.state, int(row[6])) != aad:
                raise ValueError("metadata mismatch")
            handle = metadata.state.handle
            if (hashlib.sha256(handle.encode()).hexdigest() if handle else None) != row[
                5
            ]:
                raise ValueError("handle index mismatch")
            record = ProtectedRecord(metadata, raw[4 + size :])
            if self.allocation(record) != int(row[6]):
                raise ValueError("allocation mismatch")
            return record
        except (InvalidTag, ValueError):
            raise ContinuationFailure("storage_unavailable") from None

    def save_record(
        self,
        connection: sqlite3.Connection,
        scope: str,
        caller_id: str,
        expected: ProtectedRecord | None,
        following: ProtectedRecord | None,
    ) -> None:
        """Encrypt and compare-and-swap a record within a trusted write transaction."""
        current = self.load_record(connection, scope)
        if current != expected:
            raise ContinuationFailure("busy")
        if following is None:
            connection.execute(
                "DELETE FROM continuation_records WHERE scope=?", (scope,)
            )
            return
        state = following.metadata.state
        header = following.metadata.model_dump_json().encode()
        if len(following.window) > MAX_WINDOW_BYTES or len(header) > 65536:
            raise ContinuationFailure("capacity_exceeded")
        nonce = os.urandom(12)
        allocation = self.allocation(following)
        payload = nonce + self._cipher.encrypt(
            nonce,
            len(header).to_bytes(4, "big") + header + following.window,
            self._aad(scope, caller_id, state, allocation),
        )
        count, total = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(allocation_bytes),0) "
            "FROM continuation_records"
        ).fetchone()
        previous = connection.execute(
            "SELECT allocation_bytes FROM continuation_records WHERE scope=?", (scope,)
        ).fetchone()
        if (current is None and int(count) >= self.maximum_records) or int(total) - (
            int(previous[0]) if previous else 0
        ) + allocation > self.maximum_storage_bytes:
            raise ContinuationFailure("capacity_exceeded")
        connection.execute(
            "INSERT INTO continuation_records VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(scope) DO UPDATE SET caller_id=excluded.caller_id,"
            "version=excluded.version,phase=excluded.phase,expires=excluded.expires,"
            "payload=excluded.payload,handle_digest=excluded.handle_digest,"
            "allocation_bytes=excluded.allocation_bytes",
            (
                scope,
                caller_id,
                state.version,
                state.phase,
                state.expires_at,
                payload,
                hashlib.sha256(state.handle.encode()).hexdigest()
                if state.handle
                else None,
                allocation,
            ),
        )

    def receipt(self, proof: ReceiptProof) -> ContinuationReceipt:
        """Recover only the current committed receipt after fresh signed authority."""
        with self.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            auth = self.authenticate(connection, proof)
            self.prune(connection)
            record = self.load_record(
                connection, _scope(auth.caller.scope, proof.request.conversation_id)
            )
            if record is None or record.metadata.receipt is None:
                raise ContinuationFailure("handle_unavailable")
            receipt = record.metadata.receipt
            if receipt.request_nonce != proof.request.request_nonce:
                raise ContinuationFailure("handle_unavailable")
            authorize_receipt(
                record.metadata.state,
                receipt,
                caller=auth.caller,
                owner=auth.owner,
                now=self.clock(),
            )
            self.fence(
                connection, proof.caller_id, proof.request.nonce, auth.caller.expires_at
            )
            return receipt

    @contextmanager
    def worker(self) -> Generator["CheckpointWorker"]:
        """Fence one serving process and recover abandoned attempts before work.

        Control operations such as owner revocation use separate short SQL
        transactions. The OS releases this worker lock on process death. Never
        unlink its stable file, which would let different inodes serve at once.
        """
        path = self.credentials.root / "continuation-worker.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            self.credentials.check_protected_path(path)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ContinuationFailure("busy") from None
            worker = CheckpointWorker(self)
            try:
                yield worker
            finally:
                worker.close()
        finally:
            os.close(descriptor)


class CheckpointWorker:
    """One exclusively fenced serving process with concurrent per-chat SQL leases."""

    def __init__(self, store: CheckpointStore) -> None:
        """Recover unfinished records only while the outer process fence is held."""
        self.store = store
        self.closed = False
        self.lock = threading.RLock()
        self.leases: list[CheckpointLease] = []
        with store.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            store.prune(connection)
            for (scope,) in connection.execute(
                "SELECT scope FROM continuation_records "
                "WHERE phase IN ('reserved','dispatched')"
            ).fetchall():
                record = store.load_record(connection, str(scope))
                assert record is not None
                state = interrupt(record.metadata.state)
                following = (
                    None
                    if state is None
                    else ProtectedRecord(
                        RecordMetadata(state=state, receipt=record.metadata.receipt),
                        record.window,
                    )
                )
                store.save_record(
                    connection,
                    str(scope),
                    record.metadata.state.binding.caller.subject,
                    record,
                    following,
                )

    def begin(self, proof: ContinuationProof, raw: bytes) -> "CheckpointLease":
        """Atomically verify client/policy/body, fence its nonce and reserve a turn."""
        with self.lock:
            return self._begin(proof, raw)

    def _begin(self, proof: ContinuationProof, raw: bytes) -> "CheckpointLease":
        if self.closed:
            raise ContinuationFailure("storage_unavailable")
        store = self.store
        with store.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            auth = store.authenticate(connection, proof)
            store.prune(connection)
            scope = _scope(auth.caller.scope, proof.request.conversation_id)
            existing = store.load_record(connection, scope)
            state = reserve(
                proof.request,
                raw,
                caller=auth.caller,
                owner=auth.owner,
                existing=existing.metadata.state if existing else None,
                now=store.clock(),
                lifetime_seconds=store.retention_seconds,
            )
            store.fence(
                connection, proof.caller_id, proof.request.nonce, auth.caller.expires_at
            )
            hour = int(store.clock() // 3600)
            connection.execute("DELETE FROM gateway_budget WHERE hour<?", (hour - 24,))
            budget = connection.execute(
                "SELECT attempts FROM gateway_budget WHERE hour=?", (hour,)
            ).fetchone()
            if (
                auth.settings.maximum_requests_per_hour is not None
                and budget is not None
            ):
                if int(budget[0]) >= auth.settings.maximum_requests_per_hour:
                    raise ContinuationFailure("authority_unavailable")
            connection.execute(
                "INSERT INTO gateway_budget VALUES (?,1) ON CONFLICT(hour) "
                "DO UPDATE SET attempts=attempts+1",
                (hour,),
            )
            reserved = ProtectedRecord(
                RecordMetadata(
                    state=state, receipt=existing.metadata.receipt if existing else None
                ),
                existing.window if existing else b"",
            )
            store.save_record(connection, scope, proof.caller_id, existing, reserved)
        lease = CheckpointLease(self, proof, scope, reserved)
        self.leases.append(lease)
        return lease

    def close(self) -> None:
        """Retire outstanding leases before releasing the process fence."""
        with self.lock:
            self._close()

    def _close(self) -> None:
        if self.closed:
            return
        try:
            for lease in tuple(self.leases):
                lease.close()
        finally:
            self.closed = True


class CheckpointLease:
    """Own a reserved attempt until committed or conservatively interrupted."""

    def __init__(
        self,
        worker: CheckpointWorker,
        proof: ContinuationProof,
        scope: str,
        record: ProtectedRecord,
    ) -> None:
        """Retain only one invocation's private state and exact expected version."""
        self.worker = worker
        self.proof = proof
        self.scope = scope
        self.record = record
        self.finished = False

    def dispatch(self) -> bytes:
        """Persist uncertainty before returning the private window for provider I/O."""
        with self.worker.lock:
            return self._dispatch()

    def _dispatch(self) -> bytes:
        if self.finished or self.worker.closed:
            raise ContinuationFailure("invalid_transition")
        store = self.worker.store
        with store.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            auth = store.authenticate(connection, self.proof)
            state = dispatch(
                self.record.metadata.state,
                caller=auth.caller,
                owner=auth.owner,
                now=store.clock(),
            )
            if state.handle is not None and not self.record.window:
                raise ContinuationFailure("storage_unavailable")
            following = ProtectedRecord(
                RecordMetadata(state=state, receipt=self.record.metadata.receipt),
                self.record.window,
            )
            store.save_record(
                connection, self.scope, self.proof.caller_id, self.record, following
            )
        self.record = following
        return following.window

    def commit(
        self, assistant: dict[str, JsonValue], window: bytes
    ) -> ContinuationReceipt:
        """Commit canonical provider state and public assistant history atomically."""
        with self.worker.lock:
            return self._commit(assistant, window)

    def _commit(
        self, assistant: dict[str, JsonValue], window: bytes
    ) -> ContinuationReceipt:
        if self.finished or self.worker.closed or not window:
            raise ContinuationFailure("invalid_transition")
        store = self.worker.store
        with store.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            auth = store.authenticate(connection, self.proof, admitting=False)
            state, receipt = complete(
                self.record.metadata.state,
                assistant,
                new_handle="ct1_" + secrets.token_hex(32),
                caller=auth.caller,
                owner=auth.owner,
                now=store.clock(),
            )
            following = ProtectedRecord(
                RecordMetadata(state=state, receipt=receipt), window
            )
            store.save_record(
                connection, self.scope, self.proof.caller_id, self.record, following
            )
        self.record = following
        self.finished = True
        self.worker.leases.remove(self)
        return receipt

    def close(self) -> None:
        """Release pre-dispatch work or preserve uncertainty; never refund/retry it."""
        with self.worker.lock:
            self._close()

    def _close(self) -> None:
        if self.finished:
            return
        store = self.worker.store
        try:
            with store.transaction() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = store.load_record(connection, self.scope)
                # Revocation or late cleanup must not restore a deleted/newer
                # record. Its original nonce remains fenced independently.
                if current is not None and current == self.record:
                    state = interrupt(current.metadata.state)
                    following = (
                        None
                        if state is None
                        else ProtectedRecord(
                            RecordMetadata(
                                state=state, receipt=current.metadata.receipt
                            ),
                            current.window,
                        )
                    )
                    store.save_record(
                        connection, self.scope, self.proof.caller_id, current, following
                    )
        finally:
            self.finished = True
            self.worker.leases.remove(self)
