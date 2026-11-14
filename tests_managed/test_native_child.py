"""Native SDK adoption through a real capability process and mocked upstream.

These are author-side process checks. Normally signed owner/Core installation,
original-caller authorization and real-account migration are separate gates.
"""

import asyncio
import json
import secrets
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue, TypeAdapter

from skulk_inference_reference.chat import ChatCatalog, GatewayInvocation, GatewayProof
from skulk_inference_reference.input_body import (
    BODY_MEDIA_TYPE,
    InputBody,
    StreamedCatalog,
    StreamedInvocation,
    StreamedProof,
    encode_body,
)
from skulk_openai.native_contracts import (
    build_native_manifest,
    native_bindings,
    unwrap_payload,
    wrap_payload,
)
from skulk_openai.settings import IntegrationSettings, account_binding
from tests.test_admission import invocation, prepared
from tests.test_input_body import payload
from tests_managed.native_child import NATIVE, NativeChild, native_child, output

pytestmark = pytest.mark.managed
_OBJECT = TypeAdapter(dict[str, JsonValue])
_SURFACES = TypeAdapter(list[dict[str, JsonValue]])


async def read(child: NativeChild, index: int) -> dict[str, JsonValue]:
    """Read an unchanged application message through its declared native envelope."""
    await child.invoke(index, {})
    response, body = await child.receive()
    assert response["error"] is None
    return unwrap_payload(child.bindings[index], _OBJECT.validate_json(body))


async def drain(
    child: NativeChild, reader: asyncio.StreamReader
) -> list[dict[str, JsonValue]]:
    """Require actual native end and successful control completion before assertions."""
    frames: list[dict[str, JsonValue]] = []
    while True:
        frame, media = await output(reader)
        assert not media
        if frame["kind"] == "end":
            break
        frames.append(frame)
    response, body = await child.receive()
    assert response["error"] is None and body == b"{}"
    return frames


@pytest.mark.parametrize("mock", [False, True])
def test_native_manifest_admits_all_actual_contract_bindings(mock: bool) -> None:
    """Native identities remain separate from unchanged sixteen-character proofs."""
    manifest = build_native_manifest(
        executable="bundle.pyz", executable_sha256="a" * 64, mock=mock
    )
    bindings = native_bindings(mock=mock)
    assert manifest["effect"] == "inference" and len(bindings) == 8
    assert all(
        len(binding.revision) == 64 and len(binding.legacy.revision) == 16
        for binding in bindings
    )
    assert bindings[4].declaration["id"] == bindings[6].declaration["id"]
    assert bindings[4].legacy.version != bindings[6].legacy.version
    assert bindings[4].revision != bindings[6].revision
    assert bindings[4].declaration["processing_domain"] == "external"
    assert bindings[2].declaration["processing_domain"] == "local"


async def test_native_disabled_settings_ui_and_listener_ownership(
    tmp_path: Path,
) -> None:
    """Configuration UI remains available with no account or inference permission."""
    async with native_child(tmp_path / "disconnected", enabled=False) as child:
        health = await child.health()
        surface = _SURFACES.validate_python(health["surfaces"], strict=True)[0]
        port = TypeAdapter(int).validate_python(surface["port"], strict=True)
        assert surface["url"] is None
        url = f"http://127.0.0.1:{port}/"
        assert surface["ready"] is True and "?" not in url and "#" not in url
        async with httpx.AsyncClient(trust_env=False) as browser:
            page = await browser.get(url)
            assert page.status_code == 200 and "Skulk OpenAI Integrator" in page.text
            state = _OBJECT.validate_json(
                (await browser.get(url + "api/state")).content
            )
            assert (
                state["connection"] == "disconnected"
                and state["inference_enabled"] is False
            )
        assert (await read(child, 0))["offers"] == []
        assert (await read(child, 2))["requests_started"] == 0
        assert (await child.health())["ready"] is True
    async with httpx.AsyncClient(trust_env=False) as browser:
        with pytest.raises(httpx.ConnectError):
            await browser.get(url)


async def test_native_signed_generation_and_replay_fence(tmp_path: Path) -> None:
    """Native composition does not replace app grants, nonces or owner policy."""
    capability, credential, catalog, _, clock = await prepared(tmp_path)
    async with native_child(capability.grants.credentials.root) as child:
        binding = child.bindings[1]
        signed = credential.sign(
            invocation(catalog, clock).model_copy(
                update={
                    "target_node": "synthetic-node",
                    "callable_id": binding.legacy.id,
                    "descriptor_revision": binding.legacy.revision,
                }
            )
        )
        proof = _OBJECT.validate_json(signed.model_dump_json())
        for expected in ("completed", "failed"):
            call_id = await child.invoke(1, proof)
            reader, _writer = await child.stream(call_id)
            frames = await drain(child, reader)
            assert frames[-1]["kind"] == expected
        assert (await read(child, 2))["requests_started"] == 1
        # A legacy revision is not interchangeable with the full native target.
        wrong = wrap_payload(binding, proof) | {"legacy_revision": binding.revision}
        await child.invoke(1, proof, envelope=wrong)
        refusal, body = await child.receive()
        assert refusal["error"] == "invalid_payload" and not body
        assert child.media.empty()
        assert (await read(child, 2))["requests_started"] == 1


async def test_native_both_chat_versions_and_large_media(tmp_path: Path) -> None:
    """An exact native chat version accepts practical binary input media."""
    capability, _, catalog, _, clock = await prepared(tmp_path)
    key = Ed25519PrivateKey.generate()
    capability.settings.save(
        IntegrationSettings(
            inference_enabled=True,
            processing="allow_openai",
            usage="app_settings",
            models=("text-a",),
            gateway_public_key=key.public_key().public_bytes_raw().hex(),
            account_binding=account_binding(
                catalog.registration_id, catalog.generation
            ),
        )
    )
    async with native_child(capability.grants.credentials.root) as child:
        first = ChatCatalog.model_validate_json(json.dumps(await read(child, 3)))
        invocation1 = GatewayInvocation(
            public_key=key.public_key().public_bytes_raw().hex(),
            target_node=first.target_node,
            callable_id=first.callable_id,
            descriptor_revision=first.descriptor_revision,
            catalog_revision=first.catalog_revision,
            model="text-a",
            request={"messages": [{"role": "user", "content": "write code"}]},
            nonce=secrets.token_hex(32),
            expires_at=clock() + 30,
        )
        proof1 = GatewayProof(
            invocation=invocation1, signature=key.sign(invocation1.canonical()).hex()
        )
        call_id = await child.invoke(4, _OBJECT.validate_json(proof1.model_dump_json()))
        reader, _writer = await child.stream(call_id)
        assert (await drain(child, reader))[-1]["kind"] == "completed"

        second = StreamedCatalog.model_validate_json(json.dumps(await read(child, 5)))
        raw = encode_body(payload(b"a" * 1_100_000))
        assert len(raw) > 65536
        invocation2 = StreamedInvocation(
            public_key=key.public_key().public_bytes_raw().hex(),
            target_node=second.target_node,
            callable_id=second.callable_id,
            descriptor_revision=second.descriptor_revision,
            catalog_revision=second.catalog_revision,
            model="text-a",
            body=InputBody.from_bytes(raw),
            nonce=secrets.token_hex(32),
            expires_at=clock() + 30,
        )
        proof2 = StreamedProof(
            invocation=invocation2, signature=key.sign(invocation2.canonical()).hex()
        )
        call_id = await child.invoke(6, _OBJECT.validate_json(proof2.model_dump_json()))
        reader, writer = await child.stream(call_id)
        sequence = 0

        async def send(kind: str, data: bytes = b"") -> None:
            nonlocal sequence
            frame: dict[str, JsonValue] = {
                "call_id": call_id,
                "direction": "caller_to_provider",
                "sequence": sequence,
                "kind": kind,
            }
            if kind == "chunk":
                frame.update(
                    payload={}, media={"kind": "inline", "media_type": BODY_MEDIA_TYPE}
                )
            writer.write(NATIVE.encode_transport_v2_packet(json.dumps(frame), data))
            await writer.drain()
            sequence += 1

        await send("started")
        for start in range(0, len(raw), 1_048_576):
            await send("chunk", raw[start : start + 1_048_576])
        assert (await read(child, 2))["requests_started"] == 1
        await child.health()
        await send("completed")
        frames = await drain(child, reader)
        assert frames[-1]["kind"] == "completed"
        application_events = [
            unwrap_payload(
                child.bindings[6],
                _OBJECT.validate_python(frame["payload"], strict=True),
            )
            for frame in frames
            if frame["kind"] == "chunk"
        ]
        assert application_events[-1]["kind"] == "completed"
        assert (await read(child, 2))["requests_started"] == 2


async def test_native_large_text_and_function_arguments_are_exact(
    tmp_path: Path,
) -> None:
    """Frame bounds preserve complete text and function arguments."""
    from skulk_inference_reference.chat import ChatEvent
    from tests_managed.continuation_child_fixture import LARGE_ARGUMENTS, LARGE_TEXT

    capability, _, catalog, _, clock = await prepared(tmp_path)
    key = Ed25519PrivateKey.generate()
    capability.settings.save(
        IntegrationSettings(
            inference_enabled=True,
            processing="allow_openai",
            usage="app_settings",
            models=("text-a",),
            allow_function_tools=True,
            gateway_public_key=key.public_key().public_bytes_raw().hex(),
            account_binding=account_binding(
                catalog.registration_id, catalog.generation
            ),
        )
    )
    async with native_child(
        capability.grants.credentials.root,
        entrypoint=Path(__file__).with_name("native_fixture.py"),
    ) as child:
        discovered = ChatCatalog.model_validate_json(json.dumps(await read(child, 3)))
        call = GatewayInvocation(
            public_key=key.public_key().public_bytes_raw().hex(),
            target_node=discovered.target_node,
            callable_id=discovered.callable_id,
            descriptor_revision=discovered.descriptor_revision,
            catalog_revision=discovered.catalog_revision,
            model="text-a",
            request={
                "messages": [{"role": "user", "content": "write code"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "echo", "parameters": {"type": "object"}},
                    }
                ],
            },
            nonce=secrets.token_hex(32),
            expires_at=clock() + 30,
        )
        proof = GatewayProof(
            invocation=call, signature=key.sign(call.canonical()).hex()
        )
        call_id = await child.invoke(4, _OBJECT.validate_json(proof.model_dump_json()))
        reader, _writer = await child.stream(call_id)
        frames = await drain(child, reader)
        assert frames[-1]["kind"] == "completed"
        events = [
            ChatEvent.model_validate_json(
                json.dumps(
                    unwrap_payload(
                        child.bindings[4],
                        _OBJECT.validate_python(frame["payload"], strict=True),
                    )
                )
            )
            for frame in frames
            if frame["kind"] == "chunk"
        ]
        assert events[-1].kind == "completed"
        content: list[str] = []
        arguments: list[str] = []
        for event in events:
            if event.kind != "delta":
                continue
            if "content" in event.delta:
                content.append(
                    TypeAdapter(str).validate_python(
                        event.delta["content"], strict=True
                    )
                )
            if "tool_calls" in event.delta:
                calls = TypeAdapter(list[dict[str, JsonValue]]).validate_python(
                    event.delta["tool_calls"], strict=True
                )
                for fragment in calls:
                    function = _OBJECT.validate_python(
                        fragment["function"], strict=True
                    )
                    arguments.append(
                        TypeAdapter(str).validate_python(
                            function["arguments"], strict=True
                        )
                    )
        assert "".join(content) == LARGE_TEXT
        assert "".join(arguments) == LARGE_ARGUMENTS
        assert all(len(json.dumps(frame).encode()) < 65536 for frame in frames)


async def test_populated_legacy_native_and_rollback_share_protected_history(
    tmp_path: Path,
) -> None:
    """Real runtimes preserve keys, ciphertext, nonces and CAS history."""
    from skulk_inference_reference.continuation_auth import (
        ContinuationProof,
        ReceiptProof,
    )
    from skulk_inference_reference.continuation_wire import (
        ContinuationEvent,
        ReceiptOutcome,
    )
    from skulk_openai.checkpoints import CheckpointPolicy
    from skulk_openai.managed import ContinuationConfiguration
    from skulk_openai.state_transfer import verify_snapshot
    from tests.test_checkpoints import USER, signed
    from tests_managed.test_account_child import unary as legacy_unary
    from tests_managed.test_child import managed_child
    from tests_managed.test_continuation_child import enrolled, invoke, receipt_query

    configuration = ContinuationConfiguration(
        storage=CheckpointPolicy(
            retention_seconds=300,
            maximum_records=32,
            maximum_storage_bytes=64 * 1024 * 1024,
        )
    )
    store, credential = await enrolled(tmp_path)
    proof, raw = signed(store, credential, [USER])
    async with managed_child(
        "mock-openai",
        guarded=True,
        state_directory=store.credentials.root,
        continuation=configuration,
    ) as legacy:
        event = ContinuationEvent.model_validate_json(
            json.dumps((await invoke(legacy, proof, raw))[-1])
        )
        assert event.receipt is not None
        previous = event.receipt
    identity, epoch = store.credentials.host_id(), store.credentials.epoch()

    async def native_invoke(
        child: NativeChild, proof: ContinuationProof | ReceiptProof, raw: bytes = b""
    ) -> list[dict[str, JsonValue]]:
        call_id = await child.invoke(7, _OBJECT.validate_json(proof.model_dump_json()))
        reader, writer = await child.stream(call_id)
        sequence = 0

        async def send(kind: str, data: bytes = b"") -> None:
            nonlocal sequence
            frame: dict[str, JsonValue] = {
                "call_id": call_id,
                "direction": "caller_to_provider",
                "sequence": sequence,
                "kind": kind,
            }
            if kind == "chunk":
                frame.update(
                    payload={}, media={"kind": "inline", "media_type": BODY_MEDIA_TYPE}
                )
            writer.write(NATIVE.encode_transport_v2_packet(json.dumps(frame), data))
            await writer.drain()
            sequence += 1

        await send("started")
        if raw:
            await send("chunk", raw)
        await send("completed")
        frames = await drain(child, reader)
        assert frames[-1]["kind"] == "completed"
        return [
            unwrap_payload(
                child.bindings[7],
                _OBJECT.validate_python(frame["payload"], strict=True),
            )
            for frame in frames
            if frame["kind"] == "chunk"
        ]

    public: dict[str, JsonValue] = {"role": "assistant", "content": "synthetic text"}
    following, body = signed(
        store,
        credential,
        [USER, public, {"role": "user", "content": "continue"}],
        receipt=previous,
    )
    async with native_child(
        store.credentials.root, continuation=configuration
    ) as native:
        recovered = ReceiptOutcome.model_validate_json(
            json.dumps(
                (await native_invoke(native, receipt_query(store, credential, proof)))[
                    0
                ]
            )
        )
        assert recovered.receipt == previous
        assert (await read(native, 2))["requests_started"] == 0
        event = ContinuationEvent.model_validate_json(
            json.dumps((await native_invoke(native, following, body))[-1])
        )
        assert event.receipt is not None and event.receipt.handle != previous.handle
        current = event.receipt
        assert (await read(native, 2))["requests_started"] == 1
    assert (
        store.credentials.host_id() == identity and store.credentials.epoch() == epoch
    )
    for backup in store.credentials.root.parent.glob("native-snapshot-*"):
        verify_snapshot(backup)
    # Runtime rollback keeps the advanced journal. It does not restore the old
    # snapshot or revive either already-consumed request signature.
    async with managed_child(
        "mock-openai",
        guarded=True,
        state_directory=store.credentials.root,
        continuation=configuration,
    ) as legacy:
        outcome = ReceiptOutcome.model_validate_json(
            json.dumps(
                (await invoke(legacy, receipt_query(store, credential, following)))[0]
            )
        )
        assert outcome.receipt == current
        replay = ContinuationEvent.model_validate_json(
            json.dumps((await invoke(legacy, following, body))[-1])
        )
        assert replay.event.kind == "failed"
        assert (await legacy_unary(legacy, 2))["requests_started"] == 0
