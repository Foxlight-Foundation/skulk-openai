"""Actual public SDK subprocess/media tests over real signatures and ciphertext."""

import asyncio
import json
import secrets
import signal
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue, TypeAdapter
from skulk_capability_sdk.contracts import Health, StreamHello, StreamInvoke
from skulk_capability_sdk.streams import (
    InlineMedia,
    StreamEnd,
    StreamFrame,
    read_packet,
    write_packet,
)
from skulk_capability_sdk.wire import read_message, write_message

from skulk_inference_reference.continuation_auth import (
    ContinuationCredential,
    ContinuationProof,
    ReceiptProof,
    ReceiptQuery,
)
from skulk_inference_reference.continuation_wire import (
    ContinuationEvent,
    ReceiptOutcome,
)
from skulk_inference_reference.input_body import BODY_MEDIA_TYPE, MAX_BODY_CHUNK_BYTES
from skulk_openai.checkpoints import CheckpointPolicy, CheckpointStore
from skulk_openai.continuation_client import read_key
from skulk_openai.managed import ContinuationConfiguration, descriptors
from skulk_openai.settings import IntegrationSettings, account_binding
from tests.test_admission import prepared
from tests.test_checkpoints import USER, signed
from tests.test_continuation_enrollment import cli
from tests_managed.continuation_child_fixture import LARGE_ARGUMENTS, LARGE_TEXT
from tests_managed.test_account_child import unary
from tests_managed.test_child import Child, health, managed_child

pytestmark = pytest.mark.managed
_OBJECT = TypeAdapter(dict[str, JsonValue])
_CONFIG = ContinuationConfiguration(
    storage=CheckpointPolicy(
        retention_seconds=300,
        maximum_records=32,
        maximum_storage_bytes=64 * 1024 * 1024,
    )
)
_FIXTURE = Path(__file__).with_name("continuation_child_fixture.py")


async def enrolled(tmp_path: Path) -> tuple[CheckpointStore, ContinuationCredential]:
    """Generate an account and owner-issue a client for the actual descriptor."""
    capability, _, snapshot, _, clock = await prepared(tmp_path)
    capability.settings.save(
        IntegrationSettings(
            inference_enabled=True,
            processing="allow_openai",
            usage="app_settings",
            models=("text-a",),
            allow_function_tools=True,
            account_binding=account_binding(
                snapshot.registration_id, snapshot.generation
            ),
        )
    )
    descriptor = descriptors(mock=True)[7]
    store = CheckpointStore(
        capability.grants.credentials,
        target_node="synthetic-node",
        callable_id=descriptor.id,
        descriptor_revision=descriptor.revision,
        clock=clock,
        retention_seconds=300,
        maximum_records=32,
        maximum_storage_bytes=64 * 1024 * 1024,
    )
    return store, store.issue(models=("text-a",), expires_at=clock() + 3600)


async def open_call(
    child: Child,
    proof: ContinuationProof | ReceiptProof,
    raw: bytes = b"",
    *,
    complete: bool = True,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Send typed authority on control IPC and exact body bytes on SDK media."""
    descriptor = child.manifest.descriptors[7]
    call_id = secrets.token_hex(16)
    await write_message(
        child.writer,
        StreamInvoke(
            call_id=call_id,
            capability_id=descriptor.id,
            version=descriptor.version,
            descriptor_revision=descriptor.revision,
            remaining_seconds=10.0,
            payload=_OBJECT.validate_json(proof.model_dump_json()),
        ),
    )
    reader, writer = await asyncio.wait_for(child.media.get(), 3)
    hello = await read_message(reader)
    assert isinstance(hello, StreamHello) and hello.call_id == call_id
    await write_packet(
        writer,
        StreamFrame(
            call_id=call_id, direction="caller_to_provider", sequence=0, kind="started"
        ),
    )
    sequence = 1
    for offset in range(0, len(raw), MAX_BODY_CHUNK_BYTES):
        await write_packet(
            writer,
            StreamFrame(
                call_id=call_id,
                direction="caller_to_provider",
                sequence=sequence,
                kind="chunk",
                payload={},
                media=InlineMedia(
                    data=raw[offset : offset + MAX_BODY_CHUNK_BYTES],
                    media_type=BODY_MEDIA_TYPE,
                ),
            ),
        )
        sequence += 1
    if complete:
        await write_packet(
            writer,
            StreamFrame(
                call_id=call_id,
                direction="caller_to_provider",
                sequence=sequence,
                kind="completed",
            ),
        )
    return reader, writer


async def drain(reader: asyncio.StreamReader) -> list[dict[str, JsonValue]]:
    """Require both the semantic outcome and transport terminal/cleanup marker."""
    values: list[dict[str, JsonValue]] = []
    terminal = False
    while True:
        packet = await asyncio.wait_for(read_packet(reader), 4)
        if isinstance(packet, StreamEnd):
            assert terminal
            break
        assert isinstance(packet, StreamFrame)
        if packet.kind == "completed":
            terminal = True
        else:
            assert packet.kind == "chunk" and packet.payload is not None
            values.append(packet.payload)
    assert await reader.read() == b""
    return values


async def invoke(
    child: Child, proof: ContinuationProof | ReceiptProof, raw: bytes = b""
) -> list[dict[str, JsonValue]]:
    """Own the actual authenticated media connection through cleanup."""
    reader, writer = await open_call(child, proof, raw)
    try:
        return await drain(reader)
    finally:
        writer.close()
        await writer.wait_closed()


def receipt_query(
    store: CheckpointStore, credential: ContinuationCredential, proof: ContinuationProof
) -> ReceiptProof:
    """Recover by exact attempted nonce, without requiring the lost handle."""
    return credential.receipt(
        ReceiptQuery(
            conversation_id=proof.request.conversation_id,
            target=proof.request.target,
            request_nonce=proof.request.nonce,
            nonce=secrets.token_hex(32),
            expires_at=store.clock() + 30,
        )
    )


async def test_real_child_continues_after_restart_and_recovers_receipt(
    tmp_path: Path,
) -> None:
    store, credential = await enrolled(tmp_path)
    proof, raw = signed(store, credential, [USER])
    async with managed_child(
        "mock-openai",
        guarded=True,
        state_directory=store.credentials.root,
        continuation=_CONFIG,
    ) as child:
        values = await invoke(child, proof, raw)
        event = ContinuationEvent.model_validate_json(json.dumps(values[-1]))
        receipt = event.receipt
        assert receipt is not None
        query = receipt_query(store, credential, proof)
        refused = ReceiptOutcome.model_validate_json(
            json.dumps((await invoke(child, query, b"{}"))[0])
        )
        assert refused.error is not None and refused.receipt is None
        recovered = ReceiptOutcome.model_validate_json(
            json.dumps((await invoke(child, query))[0])
        )
        assert recovered.receipt == receipt
        other = store.issue(models=("text-a",), expires_at=store.clock() + 300)
        denied = ReceiptOutcome.model_validate_json(
            json.dumps(
                (
                    await invoke(
                        child,
                        other.receipt(
                            query.request.model_copy(
                                update={"nonce": secrets.token_hex(32)}
                            )
                        ),
                    )
                )[0]
            )
        )
        assert denied.receipt is None and denied.error is not None
        assert (await unary(child, 2))["requests_started"] == 1
        await health(child)
    public: dict[str, JsonValue] = {"role": "assistant", "content": "synthetic text"}
    following, body = signed(
        store,
        credential,
        [USER, public, {"role": "user", "content": "continue"}],
        receipt=receipt,
    )
    async with managed_child(
        "mock-openai",
        guarded=True,
        state_directory=store.credentials.root,
        continuation=_CONFIG,
    ) as child:
        event = ContinuationEvent.model_validate_json(
            json.dumps((await invoke(child, following, body))[-1])
        )
        assert event.receipt is not None and event.receipt.handle != receipt.handle
        assert (await unary(child, 2))["requests_started"] == 1


@pytest.mark.parametrize("change", ["disabled", "forged", "tampered", "upload_cancel"])
async def test_unadmitted_calls_never_submit_provider_or_store_window(
    tmp_path: Path, change: str
) -> None:
    store, credential = await enrolled(tmp_path)
    proof, raw = signed(
        store, credential, [{"role": "user", "content": "x" * 1_100_000}]
    )
    if change == "forged":
        proof = proof.model_copy(update={"signature": "0" * 128})
    if change == "tampered":
        raw = raw.replace(b"user", b"tool", 1)
    async with managed_child(
        "mock-openai",
        guarded=True,
        state_directory=store.credentials.root,
        continuation=None if change == "disabled" else _CONFIG,
    ) as child:
        reader, writer = await open_call(
            child,
            proof,
            b"" if change in {"disabled", "forged"} else raw,
            complete=change != "upload_cancel",
        )
        try:
            if change != "upload_cancel":
                event = ContinuationEvent.model_validate_json(
                    json.dumps((await drain(reader))[-1])
                )
                assert event.event.kind == "failed" and event.receipt is None
                if change in {"disabled", "forged"}:
                    assert event.continuation_error is not None
            else:
                writer.close()
                await writer.wait_closed()
            await health(child)
            assert (await unary(child, 2))["requests_started"] == 0
        finally:
            writer.close()
            await writer.wait_closed()
    with store.credentials.transaction() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM continuation_records").fetchone()[
                0
            ]
            == 0
        )


async def test_large_unicode_and_function_deltas_use_real_bounded_sdk_packets(
    tmp_path: Path,
) -> None:
    store, credential = await enrolled(tmp_path)
    payload: dict[str, JsonValue] = {
        "messages": [USER],
        "tools": [
            {
                "type": "function",
                "function": {"name": "echo", "parameters": {"type": "object"}},
            }
        ],
    }
    from skulk_inference_reference.input_body import InputBody, encode_body

    raw = encode_body(payload)
    proof, _ = signed(store, credential, [USER])
    proof = credential.sign(
        proof.request.model_copy(update={"body": InputBody.from_bytes(raw)})
    )
    async with managed_child(
        "mock-openai",
        guarded=True,
        state_directory=store.credentials.root,
        continuation=_CONFIG,
        entrypoint=_FIXTURE,
    ) as child:
        values = await invoke(child, proof, raw)
        events = [
            ContinuationEvent.model_validate_json(json.dumps(value)) for value in values
        ]
        assert len(events) > 50 and events[-1].receipt is not None
        assert (
            "".join(str(event.event.delta.get("content", "")) for event in events)
            == LARGE_TEXT
        )
        arguments: list[str] = []
        for event in events:
            calls = event.event.delta.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    assert isinstance(call, dict)
                    function = call["function"]
                    assert isinstance(function, dict)
                    arguments.append(str(function["arguments"]))
        assert "".join(arguments) == LARGE_ARGUMENTS
        await health(child)


async def test_killed_child_recovers_uncertainty_without_resubmission(
    tmp_path: Path,
) -> None:
    store, credential = await enrolled(tmp_path)
    proof, raw = signed(
        store, credential, [{"role": "user", "content": "HOLD_SOURCE_FIXTURE"}]
    )
    async with managed_child(
        "mock-openai",
        guarded=True,
        state_directory=store.credentials.root,
        continuation=_CONFIG,
        entrypoint=_FIXTURE,
        expected_returncode=-signal.SIGKILL,
    ) as child:
        reader, writer = await open_call(child, proof, raw)
        packet = await asyncio.wait_for(read_packet(reader), 3)
        assert isinstance(packet, StreamFrame) and packet.payload is not None
        assert (
            ContinuationEvent.model_validate_json(json.dumps(packet.payload)).event.kind
            == "delta"
        )
        child.process.kill()
        await child.process.wait()
        writer.close()
        await writer.wait_closed()
    async with managed_child(
        "mock-openai",
        guarded=True,
        state_directory=store.credentials.root,
        continuation=_CONFIG,
    ) as child:
        retried, body = signed(
            store, credential, [{"role": "user", "content": "HOLD_SOURCE_FIXTURE"}]
        )
        event = ContinuationEvent.model_validate_json(
            json.dumps((await invoke(child, retried, body))[-1])
        )
        assert event.event.kind == "failed" and event.receipt is None
        assert (await unary(child, 2))["requests_started"] == 0
        await health(child)


async def wait_for_phase(store: CheckpointStore, phase: str) -> None:
    """Observe durable metadata only, without consuming the caller's receipt."""
    async with asyncio.timeout(3):
        while True:
            with store.credentials.transaction() as connection:
                row = connection.execute(
                    "SELECT phase FROM continuation_records"
                ).fetchone()
            if row is not None and row[0] == phase:
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("disconnected_half_close", [False, True])
async def test_lost_receipt_is_recovered_without_resubmitting_generation(
    tmp_path: Path,
    disconnected_half_close: bool,
) -> None:
    store, credential = await enrolled(tmp_path)
    proof, raw = signed(store, credential, [USER])
    async with managed_child(
        "mock-openai",
        guarded=True,
        state_directory=store.credentials.root,
        continuation=_CONFIG,
        entrypoint=Path(__file__).with_name("disconnected_half_close_fixture.py")
        if disconnected_half_close
        else None,
    ) as child:
        reader, writer = await open_call(child, proof, raw)
        packet = await asyncio.wait_for(read_packet(reader), 3)
        assert isinstance(packet, StreamFrame) and packet.payload is not None
        assert (
            ContinuationEvent.model_validate_json(json.dumps(packet.payload)).receipt
            is None
        )
        # Simulate a client losing terminal delivery after the journal commits.
        # No terminal frame or handle is consumed from the original channel.
        await wait_for_phase(store, "ready")
        writer.close()
        await writer.wait_closed()
        query = receipt_query(store, credential, proof)
        recovered = ReceiptOutcome.model_validate_json(
            json.dumps((await invoke(child, query))[0])
        )
        assert recovered.receipt is not None
        assert recovered.receipt.request_nonce == proof.request.nonce
        replay = ReceiptOutcome.model_validate_json(
            json.dumps((await invoke(child, query))[0])
        )
        assert replay.error is not None and replay.receipt is None
        assert (await unary(child, 2))["requests_started"] == 1
        await health(child)
        if disconnected_half_close:
            assert (store.credentials.root / "eof-probe").read_text() == "ENOTCONN"


async def test_media_cancellation_retires_dispatched_attempt_and_preserves_health(
    tmp_path: Path,
) -> None:
    store, credential = await enrolled(tmp_path)
    proof, raw = signed(
        store, credential, [{"role": "user", "content": "HOLD_SOURCE_FIXTURE"}]
    )
    async with managed_child(
        "mock-openai",
        guarded=True,
        state_directory=store.credentials.root,
        continuation=_CONFIG,
        entrypoint=_FIXTURE,
    ) as child:
        reader, writer = await open_call(child, proof, raw)
        packet = await asyncio.wait_for(read_packet(reader), 3)
        assert isinstance(packet, StreamFrame) and packet.payload is not None
        assert (
            ContinuationEvent.model_validate_json(json.dumps(packet.payload)).event.kind
            == "delta"
        )
        writer.close()
        await writer.wait_closed()
        await wait_for_phase(store, "interrupted")
        await health(child)
        assert (await unary(child, 2))["requests_started"] == 1
        denied = ReceiptOutcome.model_validate_json(
            json.dumps(
                (await invoke(child, receipt_query(store, credential, proof)))[0]
            )
        )
        assert denied.receipt is None and denied.error is not None
        with store.credentials.transaction() as connection:
            scope = str(
                connection.execute("SELECT scope FROM continuation_records").fetchone()[
                    0
                ]
            )
            record = store.load_record(connection, scope)
            assert (
                record is not None
                and not record.window
                and record.metadata.receipt is None
            )


async def test_client_key_owner_http_approval_cli_and_managed_media_roundtrip(
    tmp_path: Path,
) -> None:
    store, _ = await enrolled(tmp_path / "owner")
    key_file = tmp_path / "client-key.json"
    code, stdout, stderr = await cli("keygen", "--key-file", str(key_file))
    assert code == 0 and not stderr
    public = _OBJECT.validate_json(stdout)
    async with managed_child(
        "mock-openai",
        guarded=True,
        state_directory=store.credentials.root,
        continuation=_CONFIG,
    ) as child:
        await write_message(child.writer, Health(nonce="enrollment", ready=False))
        reply = await read_message(child.reader)
        assert isinstance(reply, Health) and reply.surfaces[0].url is not None
        async with httpx.AsyncClient(
            base_url=reply.surfaces[0].url, trust_env=False
        ) as browser:
            assert (await browser.get("/")).status_code == 200
            state = _OBJECT.validate_json((await browser.get("/api/state")).content)
            assert isinstance(state["csrf"], str)
            browser.headers.update(
                {
                    "Origin": reply.surfaces[0].url.rstrip("/"),
                    "X-CSRF-Token": state["csrf"],
                }
            )
            registered = await browser.post(
                "/api/clients/register",
                json={
                    **public,
                    "models": ["text-a"],
                    "expires_at": store.clock() + 600,
                },
            )
            assert registered.status_code == 200
            enrollment_file = tmp_path / "enrollment.json"
            enrollment_file.write_bytes(registered.content)
            arguments = (
                "--key-file",
                str(key_file),
                "--enrollment-file",
                str(enrollment_file),
                "--model",
                "text-a",
            )
            raw = b'{ "messages": [{"role":"user", "content":"hello"}] }\n'
            code, stdout, stderr = await cli(
                "sign", *arguments, "--conversation", "1" * 64, stdin=raw
            )
            assert code == 0 and not stderr
            proof = ContinuationProof.model_validate_json(stdout)
            event = ContinuationEvent.model_validate_json(
                json.dumps((await invoke(child, proof, raw))[-1])
            )
            assert event.receipt is not None
            code, stdout, stderr = await cli(
                "receipt",
                *arguments,
                "--conversation",
                "1" * 64,
                "--request-nonce",
                proof.request.nonce,
            )
            assert code == 0 and not stderr
            query = ReceiptProof.model_validate_json(stdout)
            recovered = ReceiptOutcome.model_validate_json(
                json.dumps((await invoke(child, query))[0])
            )
            assert recovered.receipt == event.receipt
            assert (await unary(child, 2))["requests_started"] == 1
            assert (
                await browser.post(
                    "/api/clients/revoke", json={"caller_id": proof.caller_id}
                )
            ).status_code == 200
            code, stdout, stderr = await cli(
                "sign", *arguments, "--conversation", "2" * 64, stdin=raw
            )
            assert code == 0 and not stderr
            denied = ContinuationEvent.model_validate_json(
                json.dumps(
                    (
                        await invoke(
                            child, ContinuationProof.model_validate_json(stdout), raw
                        )
                    )[-1]
                )
            )
            assert denied.event.kind == "failed" and denied.receipt is None
            assert (await unary(child, 2))["requests_started"] == 1
            await health(child)
    assert (
        read_key(key_file).private_key.get_secret_value().encode()
        not in store.credentials.path.read_bytes()
    )
