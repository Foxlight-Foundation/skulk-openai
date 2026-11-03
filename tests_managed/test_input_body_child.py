"""Exercise practical input media through the real public SDK child protocol."""

import asyncio
import json
import secrets
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue, TypeAdapter
from skulk_capability_sdk.contracts import StreamHello, StreamInvoke
from skulk_capability_sdk.streams import (
    InlineMedia,
    StreamEnd,
    StreamFrame,
    read_packet,
    write_packet,
)
from skulk_capability_sdk.wire import read_message, write_message

from skulk_inference_reference.input_body import (
    BODY_MEDIA_TYPE,
    MAX_BODY_CHUNK_BYTES,
    InputBody,
    StreamedCatalog,
    StreamedInvocation,
    StreamedProof,
    encode_body,
)
from skulk_openai.settings import IntegrationSettings, account_binding
from tests.test_admission import prepared
from tests.test_input_body import payload
from tests_managed.test_account_child import unary
from tests_managed.test_child import health, managed_child

pytestmark = pytest.mark.managed
_OBJECT = TypeAdapter(dict[str, JsonValue])


@pytest.mark.parametrize("change", ["valid", "tamper", "cancel"])
async def test_large_document_media_and_child_health(
    tmp_path: Path, change: str
) -> None:
    """Only verified completed input can start the mocked official provider."""
    capability, _, snapshot, _, clock = await prepared(tmp_path)
    key = Ed25519PrivateKey.generate()
    capability.settings.save(
        IntegrationSettings(
            inference_enabled=True,
            processing="allow_openai",
            usage="app_settings",
            models=("text-a",),
            gateway_public_key=key.public_key().public_bytes_raw().hex(),
            account_binding=account_binding(
                snapshot.registration_id, snapshot.generation
            ),
        )
    )
    async with managed_child(
        "mock-openai", guarded=True, state_directory=capability.grants.credentials.root
    ) as child:
        catalog = StreamedCatalog.model_validate_json(json.dumps(await unary(child, 5)))
        raw = encode_body(payload(b"a" * 1_100_000))
        invocation = StreamedInvocation(
            public_key=key.public_key().public_bytes_raw().hex(),
            target_node=catalog.target_node,
            callable_id=catalog.callable_id,
            descriptor_revision=catalog.descriptor_revision,
            catalog_revision=catalog.catalog_revision,
            model="text-a",
            body=InputBody.from_bytes(raw),
            nonce=secrets.token_hex(32),
            expires_at=clock() + 30,
        )
        proof = StreamedProof(
            invocation=invocation, signature=key.sign(invocation.canonical()).hex()
        )
        descriptor = child.manifest.descriptors[6]
        call_id = secrets.token_hex(16)
        await write_message(
            child.writer,
            StreamInvoke(
                call_id=call_id,
                capability_id=descriptor.id,
                version=descriptor.version,
                descriptor_revision=descriptor.revision,
                remaining_seconds=5.0,
                payload=_OBJECT.validate_json(proof.model_dump_json()),
            ),
        )
        reader, writer = await asyncio.wait_for(child.media.get(), 2)
        hello = await read_message(reader)
        assert isinstance(hello, StreamHello) and hello.call_id == call_id

        def frame(sequence: int, *, complete: bool = False) -> StreamFrame:
            return StreamFrame(
                call_id=call_id,
                direction="caller_to_provider",
                sequence=sequence,
                kind="completed" if complete else "started",
            )

        try:
            await write_packet(writer, frame(0))
            sequence = 1
            for start in range(0, len(raw), MAX_BODY_CHUNK_BYTES):
                data = raw[start : start + MAX_BODY_CHUNK_BYTES]
                if change == "tamper" and start == 0:
                    data = data.replace(b"user", b"tool", 1)
                await write_packet(
                    writer,
                    StreamFrame(
                        call_id=call_id,
                        direction="caller_to_provider",
                        sequence=sequence,
                        kind="chunk",
                        payload={},
                        media=InlineMedia(data=data, media_type=BODY_MEDIA_TYPE),
                    ),
                )
                sequence += 1
                await health(child)
                assert (await unary(child, 2))["requests_started"] == 0
                if change == "cancel":
                    writer.close()
                    await writer.wait_closed()
                    await asyncio.sleep(0.1)
                    await health(child)
                    assert (await unary(child, 2))["requests_started"] == 0
                    return
            await write_packet(writer, frame(sequence, complete=True))
            frames: list[StreamFrame] = []
            while True:
                packet = await asyncio.wait_for(read_packet(reader), 3)
                if isinstance(packet, StreamEnd):
                    break
                assert isinstance(packet, StreamFrame)
                frames.append(packet)
            outcomes = [f.payload for f in frames if f.payload]
            assert outcomes[-1]["kind"] == (
                "completed" if change == "valid" else "failed"
            )
            assert frames[-1].kind == "completed"
            await health(child)
            assert (await unary(child, 2))["requests_started"] == (
                1 if change == "valid" else 0
            )
        finally:
            writer.close()
            await writer.wait_closed()
