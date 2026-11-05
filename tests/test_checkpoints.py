"""Real caller signatures, encrypted SQL custody and abrupt-process recovery."""

import multiprocessing
import os
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import JsonValue

from skulk_inference_reference.continuation import (
    ContinuationFailure,
    ContinuationReceipt,
    ContinuationRequest,
)
from skulk_inference_reference.continuation_auth import (
    ContinuationCredential,
    ContinuationProof,
    ReceiptQuery,
)
from skulk_inference_reference.input_body import InputBody, encode_body
from skulk_openai.checkpoints import MAX_WINDOW_BYTES, CheckpointStore, ProtectedRecord
from skulk_openai.credentials import CredentialStore
from skulk_openai.provider import CatalogCache
from skulk_openai.settings import SettingsStore
from tests.test_gateway_chat import configured
from tests.test_sessions import Clock

USER: dict[str, JsonValue] = {"role": "user", "content": "Synthetic custody question"}
ANSWER: dict[str, JsonValue] = {
    "role": "assistant",
    "content": "Synthetic custody answer",
}
NEXT: dict[str, JsonValue] = {"role": "user", "content": "Continue"}
WINDOW = b"PRIVATE-CANONICAL-WINDOW\x00\xff-encrypted-upstream-item"


def storage(root: Path, now: float) -> CheckpointStore:
    """Open only a test-owned state directory with explicit storage policies."""
    return CheckpointStore(
        CredentialStore(root),
        target_node="managed.reference",
        callable_id="fixture.chat",
        descriptor_revision="a" * 16,
        clock=lambda: now,
        retention_seconds=300,
        maximum_records=32,
        maximum_storage_bytes=64 * 1024 * 1024,
    )


async def ready(
    tmp_path: Path,
) -> tuple[CheckpointStore, ContinuationCredential, Clock]:
    """Prepare a generated account; all issuer/provider HTTP uses mock transports."""
    capability, service, settings, _, _, _, clock = await configured(tmp_path)
    service.settings.save(
        settings.model_copy(update={"maximum_requests_per_hour": None})
    )
    store = CheckpointStore(
        capability.provider.sessions.store,
        target_node=capability.target_node,
        callable_id=service.callable_id,
        descriptor_revision=service.revision,
        clock=clock,
        retention_seconds=300,
        maximum_records=32,
        maximum_storage_bytes=64 * 1024 * 1024,
    )
    return store, store.issue(models=("text-a",), expires_at=clock() + 3600), clock


def signed(
    store: CheckpointStore,
    credential: ContinuationCredential,
    messages: list[dict[str, JsonValue]],
    *,
    receipt: ContinuationReceipt | None = None,
    conversation: str = "1" * 64,
) -> tuple[ContinuationProof, bytes]:
    """Sign exact full public history and independently registered caller identity."""
    raw = encode_body({"messages": list(messages)})
    request = ContinuationRequest(
        mode="continue" if receipt else "start",
        conversation_id=conversation,
        handle=receipt.handle if receipt else None,
        target=store.target.model_copy(update={"model": "text-a"}),
        body=InputBody.from_bytes(raw),
        nonce=secrets.token_hex(32),
        expires_at=store.clock() + 30,
    )
    return credential.sign(request), raw


def query(
    store: CheckpointStore,
    credential: ContinuationCredential,
    receipt: ContinuationReceipt,
) -> ContinuationReceipt:
    """Read a receipt through a distinct one-use signed operation."""
    return store.receipt(
        credential.receipt(
            ReceiptQuery(
                conversation_id=receipt.conversation_id,
                target=store.target.model_copy(update={"model": "text-a"}),
                request_nonce=receipt.request_nonce,
                nonce=secrets.token_hex(32),
                expires_at=store.clock() + 30,
            )
        )
    )


def first(
    store: CheckpointStore, credential: ContinuationCredential
) -> ContinuationReceipt:
    """Complete a synthetic turn through the real journal and client authenticator."""
    proof, raw = signed(store, credential, [USER])
    with store.worker() as worker:
        lease = worker.begin(proof, raw)
        assert lease.dispatch() == b""
        return lease.commit(ANSWER, WINDOW)


async def test_ciphertext_roundtrip_and_restart_receipt(tmp_path: Path) -> None:
    store, credential, clock = await ready(tmp_path)
    receipt = first(store, credential)
    root = store.credentials.root
    for path in root.iterdir():
        if path.is_file():
            raw = path.read_bytes()
            assert WINDOW not in raw and b"Synthetic custody" not in raw
            assert credential.private_key.get_secret_value().encode() not in raw
    assert "PRIVATE-CANONICAL" not in repr(receipt)
    assert "PRIVATE-CANONICAL" not in repr(store)
    reopened = storage(root, clock())
    assert query(reopened, credential, receipt) == receipt
    proof, raw = signed(reopened, credential, [USER, ANSWER, NEXT], receipt=receipt)
    with reopened.worker() as worker:
        lease = worker.begin(proof, raw)
        assert lease.dispatch() == WINDOW
        following = lease.commit(ANSWER, b"SECOND-PRIVATE-WINDOW")
    assert following.handle != receipt.handle
    assert query(reopened, credential, following) == following
    with pytest.raises(ContinuationFailure):
        query(reopened, credential, receipt)


@pytest.mark.parametrize(
    "denial", ["caller", "key", "body", "target", "model", "expiry"]
)
async def test_invalid_authority_never_reserves(tmp_path: Path, denial: str) -> None:
    store, credential, _ = await ready(tmp_path)
    proof, raw = signed(store, credential, [USER])
    if denial == "caller":
        other = store.issue(models=("text-a",), expires_at=store.clock() + 3600)
        proof = proof.model_copy(update={"caller_id": other.caller_id})
    elif denial == "key":
        proof = proof.model_copy(update={"signature": "0" * 128})
    elif denial == "body":
        raw += b" "
    else:
        request = proof.request
        if denial == "target":
            request = request.model_copy(
                update={
                    "target": request.target.model_copy(update={"target_node": "other"})
                }
            )
        elif denial == "model":
            request = request.model_copy(
                update={"target": request.target.model_copy(update={"model": "text-b"})}
            )
        else:
            request = request.model_copy(update={"expires_at": store.clock() - 1})
        proof = credential.sign(request)
    with store.worker() as worker:
        with pytest.raises(ContinuationFailure):
            worker.begin(proof, raw)
    with store.credentials.transaction() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM continuation_records").fetchone()[
                0
            ]
            == 0
        )


async def test_other_caller_cannot_use_handle_or_recover_receipt(
    tmp_path: Path,
) -> None:
    store, credential, _ = await ready(tmp_path)
    receipt = first(store, credential)
    other = store.issue(models=("text-a",), expires_at=store.clock() + 3600)
    proof, raw = signed(store, other, [USER, ANSWER, NEXT], receipt=receipt)
    with store.worker() as worker:
        with pytest.raises(ContinuationFailure, match="handle_unavailable"):
            worker.begin(proof, raw)
    with pytest.raises(ContinuationFailure, match="handle_unavailable"):
        query(store, other, receipt)


@pytest.mark.parametrize("phase", ["reserved", "dispatched", "committed"])
async def test_revocation_cannot_restore_or_publish_checkpoint(
    tmp_path: Path, phase: str
) -> None:
    store, credential, _ = await ready(tmp_path)
    receipt = first(store, credential)
    proof, raw = signed(store, credential, [USER, ANSWER, NEXT], receipt=receipt)
    with store.worker() as worker:
        lease = worker.begin(proof, raw)
        if phase in {"dispatched", "committed"}:
            lease.dispatch()
        if phase == "committed":
            lease.commit(ANSWER, b"replacement")
        store.revoke(credential.caller_id)
        with pytest.raises(ContinuationFailure):
            if phase == "reserved":
                lease.dispatch()
            elif phase == "dispatched":
                lease.commit(ANSWER, b"must-not-commit")
            else:
                query(store, credential, receipt)
        lease.close()
    with store.credentials.transaction() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM continuation_records").fetchone()[
                0
            ]
            == 0
        )


@pytest.mark.parametrize("phase", ["reserved", "dispatched"])
async def test_policy_edit_purges_without_late_restore(
    tmp_path: Path, phase: str
) -> None:
    store, credential, _ = await ready(tmp_path)
    receipt = first(store, credential)
    proof, raw = signed(store, credential, [USER, ANSWER, NEXT], receipt=receipt)
    with store.worker() as worker:
        lease = worker.begin(proof, raw)
        if phase == "dispatched":
            lease.dispatch()
        settings = SettingsStore(store.credentials)
        previous = settings.read()
        settings.save(previous.model_copy(update={"allow_images": True}))
        lease.close()
        settings.save(previous)
        with pytest.raises(ContinuationFailure, match="handle_unavailable"):
            worker.begin(proof, raw)
    with store.credentials.transaction() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM continuation_records").fetchone()[
                0
            ]
            == 0
        )


async def test_disconnect_purges_and_token_refresh_does_not(tmp_path: Path) -> None:
    store, credential, _ = await ready(tmp_path)
    receipt = first(store, credential)
    registration = store.credentials.load()
    assert registration is not None and registration.tokens is not None
    assert store.credentials.replace_tokens(registration, registration.tokens)
    assert query(store, credential, receipt) == receipt
    store.credentials.disconnect()
    with pytest.raises(ContinuationFailure):
        query(store, credential, receipt)
    with store.credentials.transaction() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM continuation_records").fetchone()[
                0
            ]
            == 0
        )


async def test_freshness_renewal_preserves_state(tmp_path: Path) -> None:
    store, credential, clock = await ready(tmp_path)
    receipt = first(store, credential)
    from tests.test_sessions import setup

    # Read the same generated state through an owner-local cache helper; no HTTP.
    manager, _, _ = setup(store.credentials.root.parent)
    cache = CatalogCache(manager)
    catalog = cache.read()
    assert catalog is not None
    cache.save(catalog.model_copy(update={"expires_at": clock() + 600}))
    assert query(store, credential, receipt) == receipt


async def test_nonce_and_same_conversation_races_have_one_winner(
    tmp_path: Path,
) -> None:
    store, credential, _ = await ready(tmp_path)
    proof, raw = signed(store, credential, [USER])
    with store.worker() as worker:

        def compete(_index: int) -> bool:
            try:
                worker.begin(proof, raw)
                return True
            except ContinuationFailure:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(compete, range(8)))
        assert sum(results) == 1
    with store.worker() as worker:
        with pytest.raises(ContinuationFailure):
            worker.begin(proof, raw)


def crash_worker(
    root: str, now: float, proof_json: str, raw: bytes, stage: str
) -> None:
    """Abrupt exit bypasses Python cleanup and releases only the kernel fence."""
    store = storage(Path(root), now)
    proof = ContinuationProof.model_validate_json(proof_json)
    with store.worker() as worker:
        lease = worker.begin(proof, raw)
        if stage == "dispatched":
            lease.dispatch()
            with (Path(root) / "synthetic-provider-sink").open("ab") as sink:
                sink.write(b"one-effect\n")
                sink.flush()
                os.fsync(sink.fileno())
        os._exit(73)


@pytest.mark.parametrize("stage", ["reserved", "dispatched"])
async def test_actual_process_death_never_replays_uncertain_submission(
    tmp_path: Path, stage: str
) -> None:
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
        pytest.fail("crash fixture did not exit")
    assert child.exitcode == 73
    reopened = storage(store.credentials.root, clock())
    following, new_raw = signed(
        reopened, credential, [USER, ANSWER, NEXT], receipt=receipt
    )
    with reopened.worker() as worker:
        with pytest.raises(ContinuationFailure):
            worker.begin(proof, raw)
        if stage == "dispatched":
            with pytest.raises(ContinuationFailure, match="recovery_required"):
                worker.begin(following, new_raw)
        else:
            lease = worker.begin(following, new_raw)
            assert lease.dispatch() == WINDOW
            lease.commit(ANSWER, b"after-safe-recovery")
    sink = store.credentials.root / "synthetic-provider-sink"
    assert (sink.read_bytes() if sink.exists() else b"") == (
        b"one-effect\n" if stage == "dispatched" else b""
    )


async def test_two_live_workers_cannot_both_serve(tmp_path: Path) -> None:
    store, _, clock = await ready(tmp_path)
    second = storage(store.credentials.root, clock())
    with store.worker():
        with pytest.raises(ContinuationFailure, match="busy"):
            with second.worker():
                pytest.fail("second worker acquired active fence")


@pytest.mark.parametrize("corruption", ["payload", "row", "missing_key", "wrong_key"])
async def test_corruption_and_key_loss_fail_closed(
    tmp_path: Path, corruption: str
) -> None:
    store, credential, clock = await ready(tmp_path)
    receipt = first(store, credential)
    if corruption in {"payload", "row"}:
        with store.credentials.transaction() as connection:
            if corruption == "payload":
                connection.execute(
                    "UPDATE continuation_records SET payload=randomblob(64)"
                )
            else:
                connection.execute("UPDATE continuation_records SET version=version+1")
    elif corruption == "missing_key":
        (store.credentials.root / "continuation.key").unlink()
    else:
        (store.credentials.root / "continuation.key").write_bytes(b"x" * 32)
    with pytest.raises(ContinuationFailure, match="storage_unavailable"):
        reopened = storage(store.credentials.root, clock())
        query(reopened, credential, receipt)
    with store.credentials.transaction() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM continuation_records").fetchone()[
                0
            ]
            == 1
        )


async def test_long_admitted_turn_ignores_short_proof_deadline(tmp_path: Path) -> None:
    store, credential, clock = await ready(tmp_path)
    proof, raw = signed(store, credential, [USER])
    with store.worker() as worker:
        lease = worker.begin(proof, raw)
        lease.dispatch()
        clock.now += 45
        receipt = lease.commit(ANSWER, WINDOW)
    assert query(store, credential, receipt) == receipt


async def test_receipt_query_is_one_use_and_cannot_generate(tmp_path: Path) -> None:
    store, credential, _ = await ready(tmp_path)
    receipt = first(store, credential)
    proof = credential.receipt(
        ReceiptQuery(
            conversation_id=receipt.conversation_id,
            target=store.target.model_copy(update={"model": "text-a"}),
            request_nonce=receipt.request_nonce,
            nonce=secrets.token_hex(32),
            expires_at=store.clock() + 30,
        )
    )
    assert store.receipt(proof) == receipt
    with pytest.raises(ContinuationFailure):
        store.receipt(proof)
    with pytest.raises(ValueError):
        ContinuationProof.model_validate_json(proof.model_dump_json())


async def test_storage_capacity_refusal_rolls_back_attempt_and_budget(
    tmp_path: Path,
) -> None:
    store, credential, _ = await ready(tmp_path)
    first(store, credential)
    store.maximum_records = 1
    proof, raw = signed(store, credential, [USER], conversation="2" * 64)
    with store.worker() as worker:
        with pytest.raises(ContinuationFailure, match="capacity_exceeded"):
            worker.begin(proof, raw)
    with store.credentials.transaction() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM continuation_records").fetchone()[
                0
            ]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM continuation_nonces").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT SUM(attempts) FROM gateway_budget").fetchone()[0]
            == 1
        )


async def test_oversize_checkpoint_preserves_old_window(tmp_path: Path) -> None:
    store, credential, _ = await ready(tmp_path)
    receipt = first(store, credential)
    proof, raw = signed(store, credential, [USER, ANSWER, NEXT], receipt=receipt)
    with store.worker() as worker:
        lease = worker.begin(proof, raw)
        lease.dispatch()
        with pytest.raises(ContinuationFailure, match="capacity_exceeded"):
            lease.commit(ANSWER, b"x" * (MAX_WINDOW_BYTES + 1))
        lease.close()
    with store.credentials.transaction() as connection:
        scope = connection.execute("SELECT scope FROM continuation_records").fetchone()[
            0
        ]
        record = store.load_record(connection, str(scope))
        assert record is not None and record.window == WINDOW
        assert record.metadata.state.phase == "interrupted"


async def test_handle_collision_never_deletes_another_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, credential, _ = await ready(tmp_path)
    receipt = first(store, credential)
    proof, raw = signed(store, credential, [USER], conversation="2" * 64)

    def collide(_size: int) -> str:
        return receipt.handle[4:]

    with store.worker() as worker:
        lease = worker.begin(proof, raw)
        lease.dispatch()
        with monkeypatch.context() as patch:
            patch.setattr(
                "skulk_openai.checkpoints.secrets.token_hex",
                collide,
            )
            with pytest.raises(ContinuationFailure, match="storage_unavailable"):
                lease.commit(ANSWER, b"must-not-replace")
        lease.close()
    assert query(store, credential, receipt) == receipt


async def test_failed_commit_rolls_back_receipt_and_checkpoint_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, credential, _ = await ready(tmp_path)
    receipt = first(store, credential)
    proof, raw = signed(store, credential, [USER, ANSWER, NEXT], receipt=receipt)
    original = store.save_record

    def fail_after_write(
        connection: sqlite3.Connection,
        scope: str,
        caller_id: str,
        expected: ProtectedRecord | None,
        following: ProtectedRecord | None,
    ) -> None:
        original(connection, scope, caller_id, expected, following)
        raise sqlite3.OperationalError("synthetic-private-error-must-not-escape")

    with store.worker() as worker:
        lease = worker.begin(proof, raw)
        lease.dispatch()
        with monkeypatch.context() as patch:
            patch.setattr(store, "save_record", fail_after_write)
            with pytest.raises(ContinuationFailure, match="^storage_unavailable$"):
                lease.commit(ANSWER, b"failed-new-window")
        lease.close()
    with store.credentials.transaction() as connection:
        scope = connection.execute("SELECT scope FROM continuation_records").fetchone()[
            0
        ]
        record = store.load_record(connection, str(scope))
        assert record is not None and record.window == WINDOW
        assert record.metadata.receipt == receipt
        assert record.metadata.state.phase == "interrupted"


async def test_account_replacement_revokes_clients_and_deletes_private_windows(
    tmp_path: Path,
) -> None:
    store, credential, _ = await ready(tmp_path)
    receipt = first(store, credential)
    previous = store.credentials.load()
    assert previous is not None and previous.tokens is not None
    store.credentials.connect(
        "different-synthetic-subject",
        "different-synthetic-client",
        previous.tokens,
        expected_generation=None,
        expected_epoch=store.credentials.epoch(),
    )
    with pytest.raises(ContinuationFailure):
        query(store, credential, receipt)
    with store.credentials.transaction() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM continuation_records").fetchone()[
                0
            ]
            == 0
        )
        assert (
            connection.execute(
                "SELECT enabled FROM continuation_callers WHERE id=?",
                (credential.caller_id,),
            ).fetchone()[0]
            == 0
        )


async def test_distinct_chats_can_dispatch_without_holding_database_over_provider_io(
    tmp_path: Path,
) -> None:
    store, credential, _ = await ready(tmp_path)
    proof, raw = signed(store, credential, [USER])
    other, other_raw = signed(store, credential, [USER], conversation="2" * 64)
    with store.worker() as worker:
        one = worker.begin(proof, raw)
        two = worker.begin(other, other_raw)
        one.dispatch()
        two.dispatch()
        first_receipt = one.commit(ANSWER, b"one-private-window")
        second_receipt = two.commit(ANSWER, b"two-private-window")
    assert query(store, credential, first_receipt) == first_receipt
    assert query(store, credential, second_receipt) == second_receipt
