"""Caller grants traverse a real SDK child and separately authenticated media IPC."""

import asyncio
import secrets
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter
from skulk_capability_sdk.contracts import Invoke, Result, StreamHello, StreamInvoke
from skulk_capability_sdk.streams import StreamEnd, StreamFrame, read_packet
from skulk_capability_sdk.wire import read_message, write_message

from skulk_openai.admission import CallerCredential, SignedInvocation
from skulk_openai.provider import CatalogSnapshot
from tests.test_admission import invocation, prepared
from tests.test_sessions import Clock
from tests_managed.test_child import Child, health, managed_child

pytestmark = pytest.mark.managed
_OBJECT = TypeAdapter(dict[str, JsonValue])


def sign(
    child: Child, credential: CallerCredential, catalog: CatalogSnapshot, clock: Clock
) -> SignedInvocation:
    """Bind to the actual transport target and this child's immutable descriptor."""
    descriptor = child.manifest.descriptors[1]
    return credential.sign(
        invocation(catalog, clock).model_copy(
            update={
                "target_node": "synthetic-node",
                "callable_id": descriptor.id,
                "descriptor_revision": descriptor.revision,
            }
        )
    )


async def unary(child: Child, index: int) -> dict[str, JsonValue]:
    """Read cached offers/status through the actual primary protocol channel."""
    descriptor = child.manifest.descriptors[index]
    await write_message(
        child.writer,
        Invoke(
            call_id=secrets.token_hex(16),
            capability_id=descriptor.id,
            version=descriptor.version,
            descriptor_revision=descriptor.revision,
            remaining_seconds=3.0,
            payload={},
        ),
    )
    result = await asyncio.wait_for(read_message(child.reader), 2)
    assert isinstance(result, Result) and result.error is None
    assert result.payload is not None
    return result.payload


async def open_signed(
    child: Child, signed: SignedInvocation
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Authenticate the secondary media channel for a signed inference payload."""
    descriptor = child.manifest.descriptors[1]
    call_id = secrets.token_hex(16)
    await write_message(
        child.writer,
        StreamInvoke(
            call_id=call_id,
            capability_id=descriptor.id,
            version=descriptor.version,
            descriptor_revision=descriptor.revision,
            remaining_seconds=3.0,
            payload=_OBJECT.validate_json(signed.model_dump_json()),
        ),
    )
    reader, writer = await asyncio.wait_for(child.media.get(), 2)
    hello = await asyncio.wait_for(read_message(reader), 2)
    assert isinstance(hello, StreamHello) and hello.call_id == call_id
    assert hello.token == "1" * 64
    return reader, writer


async def drain(child: Child, signed: SignedInvocation) -> list[StreamFrame]:
    """Require terminal, cleanup acknowledgement and media EOF."""
    reader, writer = await open_signed(child, signed)
    output: list[StreamFrame] = []
    try:
        while True:
            packet = await asyncio.wait_for(read_packet(reader), 2)
            if isinstance(packet, StreamEnd):
                break
            assert isinstance(packet, StreamFrame)
            output.append(packet)
        assert await asyncio.wait_for(reader.read(), 2) == b""
        return output
    finally:
        writer.close()
        await writer.wait_closed()


async def test_signed_stream_replay_and_cached_management(tmp_path: Path) -> None:
    capability, credential, catalog, _, clock = await prepared(tmp_path)
    async with managed_child(
        "mock-openai", guarded=True, state_directory=capability.grants.credentials.root
    ) as child:
        discovery = await unary(child, 0)
        assert discovery["catalog_revision"] == catalog.revision
        assert discovery["target_node"] == "synthetic-node"
        assert (await unary(child, 2))["requests_started"] == 0
        signed = sign(child, credential, catalog, clock)
        frames = await drain(child, signed)
        assert [frame.kind for frame in frames] == ["chunk", "completed"]
        await health(child)
        denied = await drain(child, signed)
        assert len(denied) == 1 and denied[0].kind == "failed"
        assert (
            denied[0].error is not None and denied[0].error.message == "policy_denied"
        )
        assert (await unary(child, 2))["requests_started"] == 1


async def test_forged_signed_direct_call_sends_no_request(tmp_path: Path) -> None:
    capability, credential, catalog, _, clock = await prepared(tmp_path)
    async with managed_child(
        "mock-openai", guarded=True, state_directory=capability.grants.credentials.root
    ) as child:
        forged = sign(child, credential, catalog, clock).model_copy(
            update={"signature": "0" * 128}
        )
        frames = await drain(child, forged)
        assert frames[-1].kind == "failed"
        assert (await unary(child, 2))["requests_started"] == 0
        await health(child)


@pytest.mark.parametrize("withdrawal", ["disabled", "disconnected"])
async def test_withdrawn_account_keeps_management_available(
    tmp_path: Path, withdrawal: str
) -> None:
    capability, credential, catalog, _, clock = await prepared(tmp_path)
    if withdrawal == "disconnected":
        await capability.provider.sessions.disconnect()
    async with managed_child(
        "mock-openai",
        guarded=True,
        enabled=withdrawal != "disabled",
        state_directory=capability.grants.credentials.root,
    ) as child:
        assert (await unary(child, 0))["offers"] == []
        frames = await drain(child, sign(child, credential, catalog, clock))
        assert frames[-1].kind == "failed"
        assert (await unary(child, 2))["requests_started"] == 0
        await health(child)


async def test_child_restart_preserves_consumed_nonce(tmp_path: Path) -> None:
    capability, credential, catalog, _, clock = await prepared(tmp_path)
    root = capability.grants.credentials.root
    async with managed_child(
        "mock-openai", guarded=True, state_directory=root
    ) as child:
        signed = sign(child, credential, catalog, clock)
        assert (await drain(child, signed))[-1].kind == "completed"
    async with managed_child(
        "mock-openai", guarded=True, state_directory=root
    ) as child:
        assert (await drain(child, signed))[-1].kind == "failed"
        assert (await unary(child, 2))["requests_started"] == 0


async def test_cancellation_keeps_primary_health_and_does_not_refund_nonce(
    tmp_path: Path,
) -> None:
    capability, credential, catalog, _, clock = await prepared(tmp_path)
    async with managed_child(
        "mock-openai", guarded=True, state_directory=capability.grants.credentials.root
    ) as child:
        signed = sign(child, credential, catalog, clock)
        reader, writer = await open_signed(child, signed)
        first = await asyncio.wait_for(read_packet(reader), 2)
        assert isinstance(first, StreamFrame) and first.kind == "chunk"
        await health(child)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.25)
        assert (await drain(child, signed))[-1].kind == "failed"
        assert (await unary(child, 2))["requests_started"] == 1
