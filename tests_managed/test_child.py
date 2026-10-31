"""Explicit SDK protocol/process tests; run only with licensed SDK access.

This harness speaks the public owner/child protocol. It is not the production
managed owner, signed installation, peer discovery or a distributed Skulk node.
"""

import asyncio
import hashlib
import os
import sys
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from skulk_capability_sdk.contracts import (
    Health,
    Hello,
    Invoke,
    Manifest,
    Result,
    Shutdown,
    Startup,
    StreamHello,
    StreamInvoke,
    canonical,
    manifest_digest,
)
from skulk_capability_sdk.streams import StreamEnd, StreamFrame, read_packet
from skulk_capability_sdk.wire import read_message, write_message

from examples.managed_child import descriptors
from skulk_openai.credentials import CredentialStore
from skulk_openai.managed import Configuration as AccountConfiguration
from skulk_openai.managed import descriptors as account_descriptors
from skulk_openai.managed import management_surfaces

pytestmark = pytest.mark.managed
_REPO = Path(__file__).resolve().parents[1]
_EXECUTABLE_DIGEST = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()


@dataclass(frozen=True)
class Child:
    """An owned synthetic child and its authenticated primary connection."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    media: asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
    manifest: Manifest
    process: asyncio.subprocess.Process


@asynccontextmanager
async def managed_child(
    profile: Literal["fixture", "mock-openai"],
    *,
    enabled: bool = True,
    delay_seconds: float = 0.0,
    guarded: bool = False,
    disconnected_live: bool = False,
    state_directory: Path | None = None,
) -> AsyncGenerator[Child]:
    """Launch one real subprocess with private temporary Unix sockets and FD IPC."""
    if disconnected_live and (
        not guarded
        or state_directory is None
        or CredentialStore(state_directory).load() is not None
    ):
        raise ValueError(
            "live-profile exercise requires an isolated disconnected store"
        )
    connections: asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        asyncio.Queue()
    )
    media: asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        asyncio.Queue()
    )
    repo = _REPO
    entry = repo / "examples/managed_child.py"
    # A short temporary root avoids Unix socket path limits on both supported OSes.
    with tempfile.TemporaryDirectory(prefix="skulk-ref-", dir="/tmp") as directory:
        root = Path(directory)
        control = await asyncio.start_unix_server(
            lambda reader, writer: connections.put_nowait((reader, writer)),
            root / "control",
        )
        stream_server = await asyncio.start_unix_server(
            lambda reader, writer: media.put_nowait((reader, writer)), root / "stream"
        )
        manifest = Manifest(
            bundle_id=(
                "reference.openai" if disconnected_live else "reference.openai.mock"
            )
            if guarded
            else f"reference.{profile}",
            bundle_version="0.1.0",
            executable=sys.executable,
            executable_sha256=_EXECUTABLE_DIGEST,
            arguments=("-m", "skulk_openai.managed") if guarded else (str(entry),),
            descriptors=account_descriptors(mock=not disconnected_live)
            if guarded
            else descriptors(profile),
            configuration_schema=AccountConfiguration.model_json_schema()
            if guarded
            else None,
            effect="governed" if guarded else "read",
            runtime_dependencies=("pyjwt",) if guarded else (),
            surfaces=management_surfaces() if guarded else (),
        )
        startup = Startup(
            socket=str(root / "control"),
            stream_socket=str(root / "stream"),
            token="1" * 64,
            node_id=f"reference.{profile}",
            host_id="synthetic-host",
            transport_id="synthetic-node",
            manifest=manifest,
            configuration={
                "inference_enabled": enabled,
                "state_directory": str(state_directory),
            }
            if guarded
            else {
                "profile": profile,
                "enabled": enabled,
                "delay_seconds": delay_seconds,
            },
        )
        process: asyncio.subprocess.Process | None = None
        writer: asyncio.StreamWriter | None = None
        try:
            with tempfile.TemporaryFile() as record:
                record.write(canonical(startup))
                record.seek(0)
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    *manifest.arguments,
                    "--config-fd",
                    str(record.fileno()),
                    pass_fds=(record.fileno(),),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env={"PATH": os.defpath, "PYTHONPATH": str(repo / "src")},
                )
            reader, writer = await asyncio.wait_for(connections.get(), 3)
            hello = await asyncio.wait_for(read_message(reader), 3)
            assert isinstance(hello, Hello)
            assert hello.token == startup.token
            assert hello.manifest_sha256 == manifest_digest(manifest)
            assert (
                hello.host_id == startup.host_id
                and hello.transport_node_id == startup.transport_id
            )
            yield Child(reader, writer, media, manifest, process)
        finally:
            if writer is not None:
                if process is not None and process.returncode is None:
                    try:
                        await write_message(writer, Shutdown())
                    except ConnectionError:
                        pass
                writer.close()
                await writer.wait_closed()
            if process is not None:
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
                if process.stderr is not None:
                    assert not await process.stderr.read(), (
                        "child failed with sanitized diagnostic"
                    )
                assert process.returncode == 0
            control.close()
            stream_server.close()
            await control.wait_closed()
            await stream_server.wait_closed()
            while not media.empty():
                _, leftover = media.get_nowait()
                leftover.close()
                await leftover.wait_closed()


async def health(child: Child) -> None:
    """Assert health remains independently responsive during and after a stream."""
    await write_message(child.writer, Health(nonce="challenge", ready=False))
    reply = await asyncio.wait_for(read_message(child.reader), 1)
    assert isinstance(reply, Health) and reply.nonce == "challenge" and reply.ready


async def open_generation(
    child: Child,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Admit synthetic input and authenticate its separately owned media channel."""
    descriptor = child.manifest.descriptors[1]
    await write_message(
        child.writer,
        StreamInvoke(
            call_id="example-call",
            capability_id=descriptor.id,
            version=descriptor.version,
            descriptor_revision=descriptor.revision,
            remaining_seconds=3.0,
            payload={"messages": [{"role": "user", "content": "hello"}]},
        ),
    )
    reader, writer = await asyncio.wait_for(child.media.get(), 2)
    hello = await asyncio.wait_for(read_message(reader), 2)
    assert isinstance(hello, StreamHello)
    assert hello.token == "1" * 64 and hello.call_id == "example-call"
    assert hello.manifest_sha256 == manifest_digest(child.manifest)
    return reader, writer


@pytest.mark.parametrize("profile", ["fixture", "mock-openai"])
async def test_real_child_stream_and_cleanup_ack(
    profile: Literal["fixture", "mock-openai"],
) -> None:
    async with managed_child(profile) as child:
        await health(child)
        reader, writer = await open_generation(child)
        try:
            first = await asyncio.wait_for(read_packet(reader), 2)
            terminal = await asyncio.wait_for(read_packet(reader), 2)
            end = await asyncio.wait_for(read_packet(reader), 2)
            assert (
                isinstance(first, StreamFrame)
                and first.kind == "chunk"
                and first.sequence == 1
            )
            assert (
                isinstance(terminal, StreamFrame)
                and terminal.kind == "completed"
                and terminal.sequence == 2
            )
            assert isinstance(end, StreamEnd) and end.call_id == "example-call"
            assert await asyncio.wait_for(reader.read(), 2) == b""
            await health(child)
        finally:
            writer.close()
            await writer.wait_closed()


async def test_same_node_id_profiles_have_distinct_callable_ids() -> None:
    async with (
        managed_child("fixture") as local,
        managed_child("mock-openai") as external,
    ):
        assert {d.qualified_id for d in local.manifest.descriptors}.isdisjoint(
            d.qualified_id for d in external.manifest.descriptors
        )
        await health(local)
        await health(external)


async def test_cancel_media_keeps_health_available_and_releases_call() -> None:
    async with managed_child("mock-openai", delay_seconds=0.3) as child:
        reader, writer = await open_generation(child)
        await health(child)
        first = await asyncio.wait_for(read_packet(reader), 2)
        assert isinstance(first, StreamFrame) and first.kind == "chunk"
        writer.close()
        await writer.wait_closed()
        # Health does not wait for the generation delay. A following call proves
        # the cancelled helper released the child's single active invocation.
        await health(child)
        await asyncio.sleep(0.05)
        reader, writer = await open_generation(child)
        try:
            assert isinstance(
                await asyncio.wait_for(read_packet(reader), 2), StreamFrame
            )
        finally:
            writer.close()
            await writer.wait_closed()


async def test_disabled_mock_withdraws_offers_and_rejects_direct_generation() -> None:
    async with managed_child("mock-openai", enabled=False) as child:
        catalog = child.manifest.descriptors[0]
        await write_message(
            child.writer,
            Invoke(
                call_id="offers",
                capability_id=catalog.id,
                version=catalog.version,
                descriptor_revision=catalog.revision,
                remaining_seconds=2.0,
                payload={},
            ),
        )
        result = await asyncio.wait_for(read_message(child.reader), 2)
        assert isinstance(result, Result) and result.payload == {"offers": []}
        reader, writer = await open_generation(child)
        try:
            terminal = await asyncio.wait_for(read_packet(reader), 2)
            assert isinstance(terminal, StreamFrame) and terminal.kind == "failed"
            assert (
                terminal.error is not None and terminal.error.message == "policy_denied"
            )
            assert isinstance(await asyncio.wait_for(read_packet(reader), 2), StreamEnd)
        finally:
            writer.close()
            await writer.wait_closed()
