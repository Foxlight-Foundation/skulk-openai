"""Caller grants traverse a real SDK child and separately authenticated media IPC."""

import asyncio
import json
import secrets
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue, TypeAdapter
from skulk_capability_sdk.contracts import (
    Health,
    Invoke,
    Result,
    StreamHello,
    StreamInvoke,
)
from skulk_capability_sdk.streams import StreamEnd, StreamFrame, read_packet
from skulk_capability_sdk.wire import read_message, write_message

from skulk_openai.admission import CallerCredential, SignedInvocation
from skulk_openai.credentials import CredentialStore
from skulk_openai.managed import build_manifest, validate_manifest
from skulk_openai.provider import CatalogSnapshot
from tests.test_admission import invocation, prepared
from tests.test_sessions import Clock
from tests_managed.test_child import Child, health, managed_child

pytestmark = pytest.mark.managed
_OBJECT = TypeAdapter(dict[str, JsonValue])


@pytest.mark.parametrize("mock", [False, True])
def test_publishing_manifest_declares_its_runtime_root(mock: bool) -> None:
    """The public factory binds profile, effect, schemas and dependency identity."""
    manifest = build_manifest(
        executable="bundle.pyz", executable_sha256="0" * 64, mock=mock
    )
    validate_manifest(manifest)
    assert manifest.bundle_id == (
        "reference.openai.mock" if mock else "reference.openai"
    )
    assert manifest.runtime_dependencies == ("pyjwt",)
    assert manifest.arguments == () and manifest.effect == "governed"


def test_publishing_manifest_refuses_a_missing_runtime_root() -> None:
    """Removing the declared library cannot leave a superficially valid bundle."""
    manifest = build_manifest(executable="bundle.pyz", executable_sha256="0" * 64)
    with pytest.raises(ValueError, match="capability contract"):
        validate_manifest(manifest.model_copy(update={"runtime_dependencies": ()}))


async def test_disconnected_live_profile_owns_idle_worker_and_shuts_down(
    tmp_path: Path,
) -> None:
    root = tmp_path / "disconnected"
    assert CredentialStore(root).load() is None


def test_publishing_manifest_refuses_an_undeclared_management_surface() -> None:
    """Removing its UI declaration cannot leave a superficially valid bundle."""
    manifest = build_manifest(executable="bundle.pyz", executable_sha256="0" * 64)
    with pytest.raises(ValueError, match="capability contract"):
        validate_manifest(manifest.model_copy(update={"surfaces": ()}))


async def test_management_surface_survives_disabled_inference_and_withdraws_on_stop(
    tmp_path: Path,
) -> None:
    """The actual child reports a credential-free URL and owns the listener."""
    root = tmp_path / "disconnected-ui"
    async with managed_child(
        "mock-openai",
        guarded=True,
        enabled=False,
        disconnected_live=True,
        state_directory=root,
    ) as child:
        await write_message(child.writer, Health(nonce="ui", ready=False))
        reply = await read_message(child.reader)
        assert isinstance(reply, Health) and reply.ready
        assert len(reply.surfaces) == 1
        surface = reply.surfaces[0]
        assert surface.surface_id == "management" and surface.ready
        assert surface.url is not None and surface.url.startswith("http://127.0.0.1:")
        assert "?" not in surface.url and "#" not in surface.url
        async with httpx.AsyncClient(trust_env=False) as browser:
            page = await browser.get(surface.url)
            assert page.status_code == 200 and "Skulk OpenAI Integrator" in page.text
            state = (await browser.get(surface.url + "api/state")).json()
            assert state["connection"] == "disconnected"
            assert not state["inference_enabled"] and state["models"] == []
        assert (await unary(child, 0))["offers"] == []
        await health(child)
    async with httpx.AsyncClient(trust_env=False) as browser:
        with pytest.raises(httpx.ConnectError):
            await browser.get(surface.url)
    async with managed_child(
        "mock-openai", guarded=True, disconnected_live=True, state_directory=root
    ) as child:
        await health(child)
        assert (await unary(child, 0))["offers"] == []
        status = await unary(child, 2)
        assert status["connection"] == "disconnected"
        assert status["requests_started"] == 0
    assert CredentialStore(root).load() is None


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


async def test_gateway_chat_uses_public_child_protocol_and_preserves_replay_fence(
    tmp_path: Path,
) -> None:
    """The real managed child discovers models and verifies gateway proof."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from skulk_inference_reference.chat import (
        ChatCatalog,
        GatewayInvocation,
        GatewayProof,
    )
    from skulk_openai.settings import IntegrationSettings, account_binding

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
    async with managed_child(
        "mock-openai", guarded=True, state_directory=capability.grants.credentials.root
    ) as child:
        discovered = ChatCatalog.model_validate_json(json.dumps(await unary(child, 3)))
        invocation = GatewayInvocation(
            public_key=key.public_key().public_bytes_raw().hex(),
            target_node=discovered.target_node,
            callable_id=discovered.callable_id,
            descriptor_revision=discovered.descriptor_revision,
            catalog_revision=discovered.catalog_revision,
            model="text-a",
            request={"messages": [{"role": "user", "content": "write code"}]},
            nonce=secrets.token_hex(32),
            expires_at=clock() + 30,
        )
        proof = GatewayProof(
            invocation=invocation, signature=key.sign(invocation.canonical()).hex()
        )

        async def run() -> list[StreamFrame]:
            descriptor = child.manifest.descriptors[4]
            call_id = secrets.token_hex(16)
            await write_message(
                child.writer,
                StreamInvoke(
                    call_id=call_id,
                    capability_id=descriptor.id,
                    version=descriptor.version,
                    descriptor_revision=descriptor.revision,
                    remaining_seconds=3.0,
                    payload=_OBJECT.validate_json(proof.model_dump_json()),
                ),
            )
            reader, writer = await asyncio.wait_for(child.media.get(), 2)
            hello = await read_message(reader)
            assert isinstance(hello, StreamHello) and hello.call_id == call_id
            frames: list[StreamFrame] = []
            try:
                while True:
                    packet = await asyncio.wait_for(read_packet(reader), 3)
                    if isinstance(packet, StreamEnd):
                        break
                    assert isinstance(packet, StreamFrame)
                    frames.append(packet)
                return frames
            finally:
                writer.close()
                await writer.wait_closed()

        frames = await run()
        assert frames[0].payload is not None and frames[0].payload["delta"] == {
            "content": "synthetic text"
        }
        assert (
            frames[-2].payload is not None and frames[-2].payload["kind"] == "completed"
        )
        assert frames[-1].kind == "completed"
        await health(child)
        denied = await run()
        assert (
            denied[0].payload is not None
            and denied[0].payload["error"] == "policy_denied"
        )
        assert (await unary(child, 2))["requests_started"] == 1


async def test_overlapping_streams_keep_the_child_healthy(tmp_path: Path) -> None:
    """Separate callers can overlap without resetting the account process."""
    capability, credential, catalog, _, clock = await prepared(tmp_path)
    async with managed_child(
        "mock-openai", guarded=True, state_directory=capability.grants.credentials.root
    ) as child:
        first = sign(child, credential, catalog, clock)
        second = credential.sign(
            first.invocation.model_copy(update={"nonce": secrets.token_hex(32)})
        )
        channels = [await open_signed(child, first), await open_signed(child, second)]

        async def consume(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> list[str]:
            kinds: list[str] = []
            try:
                while True:
                    packet = await asyncio.wait_for(read_packet(reader), 3)
                    if isinstance(packet, StreamEnd):
                        return kinds
                    assert isinstance(packet, StreamFrame)
                    kinds.append(packet.kind)
            finally:
                writer.close()
                await writer.wait_closed()

        await health(child)
        assert await asyncio.gather(*(consume(r, w) for r, w in channels)) == [
            ["chunk", "completed"],
            ["chunk", "completed"],
        ]
        await health(child)
        assert (await unary(child, 2))["requests_started"] == 2


@pytest.mark.parametrize("failure", [TimeoutError, ConnectionResetError])
async def test_overload_media_failure_preserves_active_calls_and_primary_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    """Stalled/rejected media never blocks primary health or cancels other calls."""
    import tempfile

    from skulk_capability_sdk.contracts import Hello, Shutdown, Startup
    from skulk_capability_sdk.streams import StreamHandler

    from skulk_openai import managed

    entered: asyncio.Queue[str] = asyncio.Queue()
    rejected: asyncio.Queue[str] = asyncio.Queue()
    release_rejections = asyncio.Event()
    active = asyncio.Event()
    normal_cancelled: set[str] = set()

    async def exchange(
        startup: Startup, call: StreamInvoke, handler: StreamHandler
    ) -> None:
        _ = startup, handler
        entered.put_nowait(call.call_id)
        if int(call.call_id) > 4:
            await release_rejections.wait()
            rejected.put_nowait(call.call_id)
            raise failure()
        try:
            await active.wait()
        except asyncio.CancelledError:
            normal_cancelled.add(call.call_id)
            raise

    monkeypatch.setattr(managed, "execute_stream", exchange)
    connections: asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        asyncio.Queue()
    )
    with tempfile.TemporaryDirectory(prefix="skulk-overload-", dir="/tmp") as directory:
        socket = Path(directory) / "control"
        server = await asyncio.start_unix_server(
            lambda reader, writer: connections.put_nowait((reader, writer)), socket
        )
        manifest = build_manifest(
            executable="bundle.pyz", executable_sha256="0" * 64, mock=True
        )
        startup = Startup(
            socket=str(socket),
            stream_socket=str(Path(directory) / "media"),
            token="1" * 64,
            host_id="synthetic-host",
            transport_id="synthetic-node",
            node_id="reference.openai.mock",
            manifest=manifest,
            configuration={
                "state_directory": str(tmp_path),
                "inference_enabled": False,
            },
        )
        child = asyncio.create_task(managed.serve(startup))
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(connections.get(), 2)
            assert isinstance(await read_message(reader), Hello)
            descriptor = manifest.descriptors[1]
            for index in range(1, 10):
                await write_message(
                    writer,
                    StreamInvoke(
                        call_id=str(index),
                        capability_id=descriptor.id,
                        version=descriptor.version,
                        descriptor_revision=descriptor.revision,
                        remaining_seconds=3.0,
                        payload={},
                    ),
                )
                if index < 9:
                    assert await asyncio.wait_for(entered.get(), 1) == str(index)
            # Four normal executions and at most four rejection exchanges.
            # The ninth invocation must not allocate another task.
            await write_message(writer, Health(nonce="overloaded", ready=False))
            reply = await asyncio.wait_for(read_message(reader), 1)
            assert (
                isinstance(reply, Health)
                and reply.ready
                and reply.nonce == "overloaded"
            )
            assert entered.empty() and not normal_cancelled
            release_rejections.set()
            for _ in range(4):
                await asyncio.wait_for(rejected.get(), 1)
            await write_message(writer, Health(nonce="after-failure", ready=False))
            reply = await asyncio.wait_for(read_message(reader), 1)
            assert isinstance(reply, Health) and reply.nonce == "after-failure"
            assert not normal_cancelled and not child.done()
            await write_message(writer, Shutdown())
            await asyncio.wait_for(child, 2)
            assert normal_cancelled == {"1", "2", "3", "4"}
        finally:
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            server.close()
            await server.wait_closed()
