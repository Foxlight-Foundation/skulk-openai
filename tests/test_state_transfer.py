"""Populated private snapshots, native enrollment and quarantined recovery.

All account keys, provider responses and checkpoint windows are synthetic.
Tests inspect actual SQL, AES-GCM records, signatures and stable process fences.
"""

import multiprocessing
import secrets
import sqlite3
from pathlib import Path

import pytest

from skulk_inference_reference.continuation import ContinuationFailure
from skulk_openai.auth_contracts import AuthFailure
from skulk_openai.credentials import CredentialStore
from skulk_openai.settings import SettingsStore
from skulk_openai.state_transfer import (
    AdoptionBinding,
    NativeAdoption,
    application_fence,
    enroll_native,
    read_native_adoption,
    restore_quarantined,
    snapshot_state,
    verify_snapshot,
)
from tests.test_checkpoints import (
    ANSWER,
    NEXT,
    USER,
    WINDOW,
    crash_worker,
    first,
    query,
    ready,
    signed,
    storage,
)


def binding() -> AdoptionBinding:
    """Pair explicit test identities without claiming they have a common digest."""
    return AdoptionBinding(
        legacy_id="fixture.chat",
        version="0.1.0",
        legacy_revision="a" * 16,
        native_id="fixture.native",
        native_revision="b" * 64,
    )


async def test_populated_snapshot_and_same_journal_rollback(tmp_path: Path) -> None:
    """Enrollment preserves authority history and readable ciphertext."""
    store, credential, clock = await ready(tmp_path)
    receipt = first(store, credential)
    assert query(store, credential, receipt) == receipt
    root = store.credentials.root
    with store.credentials.transaction() as connection:
        previous_version = int(
            connection.execute("SELECT version FROM continuation_records").fetchone()[0]
        )
    old_identity = store.credentials.host_id()
    old_epoch = store.credentials.epoch()
    backup = tmp_path / "snapshot"
    snapshot = snapshot_state(store.credentials, backup)
    assert verify_snapshot(backup) == snapshot
    assert WINDOW not in (backup / "credentials.sqlite3").read_bytes()
    assert (backup / "continuation.key").read_bytes() == (
        root / "continuation.key"
    ).read_bytes()
    adoption = NativeAdoption(
        store_host_id=old_identity,
        legacy_target_node="managed.reference",
        native_node_id="native-installed-child",
        native_bundle_id="reference.openai.native",
        bindings=(binding(),),
        snapshot_state_digest=snapshot.state_digest,
    )
    enroll_native(store.credentials, backup, adoption)
    assert read_native_adoption(root) == adoption
    assert (
        store.credentials.host_id() == old_identity
        and store.credentials.epoch() == old_epoch
    )
    assert len(adoption.bindings[0].legacy_revision) == 16
    assert len(adoption.bindings[0].native_revision) == 64
    # Rolling back the runtime reopens the current journal, never the snapshot.
    reopened = storage(root, clock())
    assert query(reopened, credential, receipt) == receipt
    following, raw = signed(reopened, credential, [USER, ANSWER, NEXT], receipt=receipt)
    with reopened.worker() as worker:
        lease = worker.begin(following, raw)
        assert lease.dispatch() == WINDOW
        current = lease.commit(ANSWER, b"following-window")
    with reopened.credentials.transaction() as connection:
        assert (
            int(
                connection.execute(
                    "SELECT version FROM continuation_records"
                ).fetchone()[0]
            )
            > previous_version
        )
    assert query(reopened, credential, current) == current
    assert verify_snapshot(backup) == snapshot


@pytest.mark.parametrize("fence", ["application", "checkpoint"])
async def test_snapshot_refuses_a_serving_process(tmp_path: Path, fence: str) -> None:
    """Neither the new app fence nor a legacy checkpoint worker can be bypassed."""
    store, _, _ = await ready(tmp_path)
    context = (
        application_fence(store.credentials.root)
        if fence == "application"
        else store.worker()
    )
    with context:
        with pytest.raises(AuthFailure):
            snapshot_state(store.credentials, tmp_path / "busy-snapshot")
    assert not (tmp_path / "busy-snapshot/snapshot.json").exists()


async def test_restore_rechecks_actual_copy_before_releasing_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup modified after initial verification leaves an unservable restore."""
    import skulk_openai.state_transfer as transfer

    store, credential, _ = await ready(tmp_path)
    first(store, credential)
    backup = tmp_path / "snapshot"
    snapshot_state(store.credentials, backup)
    verify = transfer.verify_snapshot

    def changed_source(root: Path) -> transfer.StateSnapshot:
        result = verify(root)
        with sqlite3.connect(root / "credentials.sqlite3") as connection:
            connection.execute(
                "UPDATE metadata SET value=CAST(value AS INTEGER)+1 "
                "WHERE key='auth_epoch'"
            )
        return result

    monkeypatch.setattr(transfer, "verify_snapshot", changed_source)
    destination = tmp_path / "changed-copy"
    with pytest.raises(AuthFailure):
        restore_quarantined(backup, destination)
    assert (destination / "state-transfer.pending").exists()
    assert not (destination / "credentials.sqlite3").exists()
    with pytest.raises(AuthFailure):
        CredentialStore(destination)


async def test_adoption_refuses_a_backup_older_than_revocation(tmp_path: Path) -> None:
    """A stale snapshot cannot roll back a caller revocation or its deleted records."""
    store, credential, _ = await ready(tmp_path)
    first(store, credential)
    backup = tmp_path / "snapshot"
    snapshot = snapshot_state(store.credentials, backup)
    store.revoke(credential.caller_id)
    adoption = NativeAdoption(
        store_host_id=store.credentials.host_id(),
        legacy_target_node="managed.reference",
        native_node_id="new-child",
        native_bundle_id="reference.openai.native",
        bindings=(binding(),),
        snapshot_state_digest=snapshot.state_digest,
    )
    with pytest.raises(AuthFailure):
        enroll_native(store.credentials, backup, adoption)
    assert not (store.credentials.root / "native-adoption.json").exists()
    with store.credentials.transaction() as connection:
        assert connection.execute(
            "SELECT enabled FROM continuation_callers"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM continuation_records"
        ).fetchone() == (0,)


@pytest.mark.parametrize("mismatch", ["target", "callable"])
async def test_adoption_refuses_rebinding_populated_history(
    tmp_path: Path, mismatch: str
) -> None:
    """A coherent backup cannot authorize changing encrypted checkpoint targets."""
    store, credential, _ = await ready(tmp_path)
    receipt = first(store, credential)
    backup = tmp_path / "snapshot"
    snapshot = snapshot_state(store.credentials, backup)
    adoption = NativeAdoption(
        store_host_id=store.credentials.host_id(),
        legacy_target_node="foreign-target"
        if mismatch == "target"
        else "managed.reference",
        native_node_id="new-child",
        native_bundle_id="reference.openai.native",
        bindings=(
            binding().model_copy(update={"legacy_id": "foreign.callable"})
            if mismatch == "callable"
            else binding(),
        ),
        snapshot_state_digest=snapshot.state_digest,
    )
    with pytest.raises(AuthFailure):
        enroll_native(store.credentials, backup, adoption)
    assert not (store.credentials.root / "native-adoption.json").exists()
    assert query(store, credential, receipt) == receipt


@pytest.mark.parametrize("corruption", ["key", "database", "missing_key", "loose_mode"])
async def test_backup_tampering_refuses_before_restore(
    tmp_path: Path, corruption: str
) -> None:
    """Mismatched, incomplete and publicly accessible backup files cannot restore."""
    store, credential, _ = await ready(tmp_path)
    first(store, credential)
    backup = tmp_path / "snapshot"
    snapshot_state(store.credentials, backup)
    if corruption == "key":
        (backup / "continuation.key").write_bytes(secrets.token_bytes(32))
    elif corruption == "database":
        with (backup / "credentials.sqlite3").open("ab") as output:
            output.write(b"tampered")
    elif corruption == "missing_key":
        (backup / "continuation.key").unlink()
    else:
        (backup / "snapshot.json").chmod(0o644)
    destination = tmp_path / "restored"
    with pytest.raises(AuthFailure):
        restore_quarantined(backup, destination)
    assert not destination.exists()


async def test_disaster_restore_preserves_ciphertext_and_quarantines_authority(
    tmp_path: Path,
) -> None:
    """A new root retains CAS/nonces but cannot revive pre-backup signing authority."""
    store, credential, clock = await ready(tmp_path)
    receipt = first(store, credential)
    assert query(store, credential, receipt) == receipt
    root = store.credentials.root
    with store.credentials.transaction() as connection:
        original_nonces = connection.execute(
            "SELECT * FROM continuation_nonces ORDER BY nonce"
        ).fetchall()
        original_records = connection.execute(
            "SELECT * FROM continuation_records"
        ).fetchall()
    backup = tmp_path / "snapshot"
    snapshot_state(store.credentials, backup)
    recovered = restore_quarantined(backup, tmp_path / "recovered")
    assert recovered.host_id() == store.credentials.host_id()
    assert recovered.epoch() > store.credentials.epoch()
    assert recovered.load() is not None
    settings = SettingsStore(recovered).read()
    assert not settings.inference_enabled and settings.gateway_public_key is None
    assert not (recovered.root / "native-adoption.json").exists()
    assert (recovered.root / "continuation.key").read_bytes() == (
        root / "continuation.key"
    ).read_bytes()
    with recovered.transaction() as connection:
        assert (
            connection.execute(
                "SELECT * FROM continuation_nonces ORDER BY nonce"
            ).fetchall()
            == original_nonces
        )
        assert (
            connection.execute("SELECT * FROM continuation_records").fetchall()
            == original_records
        )
        assert connection.execute(
            "SELECT enabled,remaining FROM grants"
        ).fetchall() == [(0, 0)]
        assert connection.execute(
            "SELECT enabled FROM continuation_callers"
        ).fetchall() == [(0,)]
    reopened = storage(recovered.root, clock())
    with pytest.raises(ContinuationFailure):
        query(reopened, credential, receipt)
    with pytest.raises(FileExistsError):
        restore_quarantined(backup, root)


@pytest.mark.parametrize("stage", ["reserved", "dispatched"])
async def test_backup_restore_does_not_replay_abruptly_stopped_work(
    tmp_path: Path, stage: str
) -> None:
    """Actual process death retains the sealed uncertainty record and nonce history."""
    store, credential, clock = await ready(tmp_path)
    receipt = first(store, credential)
    proof, raw = signed(store, credential, [USER, ANSWER, NEXT], receipt=receipt)
    child = multiprocessing.get_context("spawn").Process(
        target=crash_worker,
        args=(
            str(store.credentials.root),
            clock(),
            proof.model_dump_json(),
            raw,
            stage,
        ),
    )
    child.start()
    child.join(timeout=20)
    if child.is_alive():
        child.kill()
        child.join(timeout=5)
        pytest.fail("owned crash fixture did not exit")
    assert child.exitcode == 73
    backup = tmp_path / "snapshot"
    snapshot_state(store.credentials, backup)
    recovered = restore_quarantined(backup, tmp_path / "recovered")
    reopened = storage(recovered.root, clock())
    with reopened.worker() as worker:
        with pytest.raises(ContinuationFailure):
            worker.begin(proof, raw)
    original_sink = store.credentials.root / "synthetic-provider-sink"
    assert (original_sink.read_bytes() if original_sink.exists() else b"") == (
        b"one-effect\n" if stage == "dispatched" else b""
    )
    assert not (recovered.root / "synthetic-provider-sink").exists()


async def test_interrupted_restore_stays_unservable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash before quarantine completion cannot expose restored caller authority."""
    from skulk_openai import state_transfer

    store, credential, _ = await ready(tmp_path)
    first(store, credential)
    backup = tmp_path / "snapshot"
    snapshot_state(store.credentials, backup)

    def fail(_database: Path) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(state_transfer, "_quarantine", fail)
    destination = tmp_path / "incomplete"
    with pytest.raises(OSError):
        restore_quarantined(backup, destination)
    assert (destination / "state-transfer.pending").exists()
    assert not (destination / "credentials.sqlite3").exists()
    with pytest.raises(AuthFailure):
        CredentialStore(destination)
    assert store.credentials.load() is not None
    verify_snapshot(backup)
