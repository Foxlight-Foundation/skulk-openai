"""Read pre-upgrade encrypted custody and signatures without legacy dependencies.

The frozen synthetic fixture was produced with cryptography 46.0.7 and the 0.5.0
checkpoint implementation. Regenerating it with the current dependency would
erase the cross-version regression this test is meant to preserve.
"""

import hashlib
import os
from dataclasses import replace
from pathlib import Path
from typing import final

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from skulk_inference_reference.contracts import FrozenContract
from skulk_openai.checkpoints import CheckpointStore, ProtectedRecord, RecordMetadata
from skulk_openai.credentials import CredentialStore


@final
class LegacyFixture(FrozenContract):
    """Only synthetic values, including a deliberately public test encryption key."""

    producer: str
    scope: str
    caller_id: str
    metadata: RecordMetadata
    window_hex: str
    key_hex: str
    payload_hex: str
    allocation_bytes: int
    signed_message_hex: str
    public_key_hex: str
    signature_hex: str


def legacy_fixture() -> LegacyFixture:
    """Load bytes created before the cryptography dependency upgrade."""
    return LegacyFixture.model_validate_json(
        (Path(__file__).parent / "fixtures/crypto-46-checkpoint-v1.json").read_bytes()
    )


def open_store(root: Path) -> CheckpointStore:
    """Open a test-owned journal with the fixture's unchanged destination binding."""
    return CheckpointStore(
        CredentialStore(root),
        target_node="managed.reference",
        callable_id="fixture.chat",
        descriptor_revision="a" * 16,
        clock=lambda: 1000.0,
        retention_seconds=300,
        maximum_records=32,
        maximum_storage_bytes=64 * 1024 * 1024,
    )


def test_upgrade_reads_and_rewrites_existing_checkpoint(tmp_path: Path) -> None:
    fixture = legacy_fixture()
    credentials = CredentialStore(tmp_path / "state")
    key_path = credentials.root / "continuation.key"
    descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as key_file:
        key_file.write(bytes.fromhex(fixture.key_hex))
    store = open_store(credentials.root)
    state = fixture.metadata.state
    assert state.handle is not None
    expected = ProtectedRecord(fixture.metadata, bytes.fromhex(fixture.window_hex))
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO continuation_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fixture.scope,
                fixture.caller_id,
                state.version,
                state.phase,
                state.expires_at,
                bytes.fromhex(fixture.payload_hex),
                hashlib.sha256(state.handle.encode()).hexdigest(),
                fixture.allocation_bytes,
            ),
        )
        assert store.load_record(connection, fixture.scope) == expected
        following = ProtectedRecord(
            fixture.metadata.model_copy(
                update={"state": replace(state, version=state.version + 1)}
            ),
            b"SYNTHETIC-UPGRADED-WINDOW\x00\xff",
        )
        store.save_record(
            connection, fixture.scope, fixture.caller_id, expected, following
        )
    reopened = open_store(credentials.root)
    with reopened.transaction() as connection:
        assert reopened.load_record(connection, fixture.scope) == following
    assert key_path.read_bytes() == bytes.fromhex(fixture.key_hex)


def test_upgrade_verifies_existing_ed25519_signatures() -> None:
    fixture = legacy_fixture()
    public_key = Ed25519PublicKey.from_public_bytes(
        bytes.fromhex(fixture.public_key_hex)
    )
    signature = bytes.fromhex(fixture.signature_hex)
    message = bytes.fromhex(fixture.signed_message_hex)
    public_key.verify(signature, message)
    with pytest.raises(InvalidSignature):
        public_key.verify(signature, message + b"changed")
