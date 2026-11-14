"""Independent public-ABI process driver; no production host-authority claim."""

import asyncio
import hashlib
import importlib
import json
import os
import secrets
import sys
import tempfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from pydantic import JsonValue, TypeAdapter

from skulk_openai.credentials import CredentialStore
from skulk_openai.managed import ContinuationConfiguration
from skulk_openai.native_contracts import (
    NativeBinding,
    NativeKernel,
    NativeRecord,
    adoption_bindings,
    build_native_manifest,
    native_bindings,
    wrap_payload,
)
from skulk_openai.state_transfer import NativeAdoption, enroll_native, snapshot_state

_OBJECT = TypeAdapter(dict[str, JsonValue])
_ROOT = Path(__file__).resolve().parents[1]
_EXECUTABLE_SHA256 = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()


class ABI(NativeKernel, Protocol):
    """The real public wire codec methods used by independent test infrastructure."""

    def calls_v2_profile_digest(self) -> str: ...
    def composition_v3_profile_digest(self) -> str: ...
    def encode_call_v2_frame(self, source: str, payload: bytes) -> bytes: ...
    def decode_call_v2_frame(self, packet: bytes) -> tuple[str, bytes]: ...
    def call_v2_payload_size(self, source: str) -> int: ...
    def encode_transport_v2_packet(self, source: str, media: bytes) -> bytes: ...
    def decode_transport_v2_packet(self, packet: bytes) -> tuple[str, bytes]: ...


NATIVE = cast(ABI, cast(object, importlib.import_module("_skulk_capability_kernel")))


async def header(reader: asyncio.StreamReader) -> bytes:
    """Bound allocation before reading a native packet header."""
    size = int.from_bytes(await reader.readexactly(4), "big")
    assert 2 <= size <= 262144
    return await reader.readexactly(size)


@dataclass(frozen=True)
class NativeChild:
    """One real capability subprocess and independent native control/media readers."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    media: asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
    bindings: tuple[NativeBinding, ...]
    startup: NativeRecord
    process: asyncio.subprocess.Process

    async def send(self, kind: str, body: bytes = b"", **fields: JsonValue) -> None:
        """Encode complete native control frames with the actual released codec."""
        self.writer.write(
            NATIVE.encode_call_v2_frame(
                json.dumps(
                    {
                        "protocol": 2,
                        "contract_profile": "skulk.calls/2",
                        "profile_digest": NATIVE.calls_v2_profile_digest(),
                        "kind": kind,
                        "payload_size": len(body),
                        **fields,
                    }
                ),
                body,
            )
        )
        await self.writer.drain()

    async def receive(self) -> tuple[dict[str, JsonValue], bytes]:
        """Read and independently re-admit a whole compiled response frame."""
        raw = await asyncio.wait_for(header(self.reader), 5)
        body = await self.reader.readexactly(NATIVE.call_v2_payload_size(raw.decode()))
        value, payload = NATIVE.decode_call_v2_frame(
            len(raw).to_bytes(4, "big") + raw + body
        )
        return _OBJECT.validate_json(value), payload

    async def invoke(
        self,
        index: int,
        payload: dict[str, JsonValue],
        *,
        envelope: dict[str, JsonValue] | None = None,
    ) -> str:
        """Bind raw request bytes and the exact native target to an original caller."""
        binding = self.bindings[index]
        value = binding.declaration
        target = {
            "node_id": "native-synthetic-child",
            "capability_id": value["id"],
            "version": value["version"],
            "descriptor_revision": binding.revision,
            "behavior": value["behavior"],
            "processing_domain": value["processing_domain"],
        }
        raw = json.dumps(
            wrap_payload(binding, payload) if envelope is None else envelope
        ).encode()
        scope, call_id = secrets.token_hex(32), secrets.token_hex(16)
        context: dict[str, JsonValue] = {
            "protocol": 3,
            "contract_profile": "skulk.composition/3",
            "profile_digest": NATIVE.composition_v3_profile_digest(),
            "kind": "context",
            "caller_id": "client:synthetic",
            "caller_kind": "client",
            "issuer_node_id": "core-profile:synthetic",
            "policy_revision": "a" * 64,
            "root_scope_id": scope,
            "scope_id": scope,
            "parent_scope_id": None,
            "executor": target,
            "allowed_targets": [target],
            "allowed_processing_domains": [value["processing_domain"]],
            "ancestry": [],
            "request_sha256": hashlib.sha256(raw).hexdigest(),
            "deadline_unix_ms": None,
        }
        await self.send(
            "invoke",
            raw,
            call_id=call_id,
            mode=value["io_mode"],
            context=context,
            remaining_seconds=None,
        )
        return call_id

    async def health(self) -> dict[str, JsonValue]:
        """Ask the actual sole control reader for current setup/UI readiness."""
        await self.send("health", nonce="probe", ready=False, surfaces=[])
        value, body = await self.receive()
        assert (
            value["kind"] == "health"
            and value["nonce"] == "probe"
            and value["ready"] is True
            and not body
        )
        return value

    async def stream(
        self, call_id: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Match the actual compiled startup-bound stream hello before consuming it."""
        reader, writer = await asyncio.wait_for(self.media.get(), 5)
        self.startup.admit_hello((await header(reader)).decode(), call_id)
        return reader, writer


async def output(reader: asyncio.StreamReader) -> tuple[dict[str, JsonValue], bytes]:
    """Re-admit a full native media packet using the real immutable decoder."""
    raw = await asyncio.wait_for(header(reader), 5)
    envelope = _OBJECT.validate_json(raw)
    size = TypeAdapter(int).validate_python(envelope["media_size"], strict=True)
    body = await reader.readexactly(size)
    value, media = NATIVE.decode_transport_v2_packet(
        len(raw).to_bytes(4, "big") + raw + body
    )
    return _OBJECT.validate_json(value), media


@asynccontextmanager
async def native_child(
    state: Path,
    *,
    enabled: bool = True,
    continuation: ContinuationConfiguration | None = None,
    entrypoint: Path | None = None,
) -> AsyncGenerator[NativeChild]:
    """Own one account-free process; always drain it and every test socket."""
    connected: asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        asyncio.Queue()
    )
    media: asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        asyncio.Queue()
    )
    writers: list[asyncio.StreamWriter] = []

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        connected.put_nowait((reader, writer))

    def accept_media(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writers.append(writer)
        media.put_nowait((reader, writer))

    if not (state / "native-adoption.json").exists():
        store = CredentialStore(state)
        backup = state.parent / ("native-snapshot-" + secrets.token_hex(8))
        snapshot = snapshot_state(store, backup)
        enroll_native(
            store,
            backup,
            NativeAdoption(
                store_host_id=store.host_id(),
                legacy_target_node="synthetic-node",
                native_node_id="native-synthetic-child",
                native_bundle_id="reference.openai.native.mock",
                bindings=adoption_bindings(mock=True),
                snapshot_state_digest=snapshot.state_digest,
            ),
        )
    with tempfile.TemporaryDirectory(prefix="native-oai-", dir="/tmp") as directory:
        root = Path(directory)
        control = await asyncio.start_unix_server(accept, root / "control")
        streams = await asyncio.start_unix_server(accept_media, root / "stream")
        manifest = build_native_manifest(
            executable=Path(sys.executable).name,
            executable_sha256=_EXECUTABLE_SHA256,
            mock=True,
        )
        declared = NATIVE.ManagedV2Contract.from_json("Manifest", json.dumps(manifest))
        value: dict[str, JsonValue] = {
            "protocol": 6,
            "contract_profile": "skulk.managed/2",
            "profile_digest": NATIVE.managed_v2_profile_digest(),
            "socket": str(root / "control"),
            "stream_socket": str(root / "stream"),
            "token": "c" * 64,
            "node_id": "native-synthetic-child",
            "host_id": "synthetic-owner",
            "transport_id": "synthetic-core",
            "executable": sys.executable,
            "manifest": manifest,
            "manifest_digest": declared.digest,
            "configuration": {
                "legacy_target_node": "synthetic-node",
                "application": {
                    "state_directory": str(state),
                    "inference_enabled": enabled,
                    "continuation": _OBJECT.validate_json(
                        continuation.model_dump_json()
                    )
                    if continuation is not None
                    else None,
                },
            },
            "credential_config": None,
            "serve_host": None,
        }
        startup = NATIVE.ManagedV2Contract.from_json("Startup", json.dumps(value))
        child: NativeChild | None = None
        with tempfile.TemporaryFile() as record:
            record.write(startup.canonical())
            record.seek(0)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                *(
                    ("-m", "skulk_openai.native_managed")
                    if entrypoint is None
                    else (str(entrypoint),)
                ),
                "--mock",
                "--config-fd",
                str(record.fileno()),
                pass_fds=(record.fileno(),),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env={
                    "PATH": os.defpath,
                    "PYTHONPATH": os.pathsep.join((str(_ROOT / "src"), str(_ROOT))),
                },
            )
        try:
            reader, writer = await asyncio.wait_for(connected.get(), 5)
            startup.admit_hello((await header(reader)).decode())
            child = NativeChild(
                reader, writer, media, native_bindings(mock=True), startup, process
            )
            yield child
        finally:
            if child is not None and process.returncode is None:
                try:
                    await child.send("shutdown")
                except (ConnectionError, ValueError):
                    pass
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()
            for writer in writers:
                writer.close()
                await writer.wait_closed()
            control.close()
            streams.close()
            await control.wait_closed()
            await streams.wait_closed()
            assert process.stderr is not None
            diagnostics = await process.stderr.read()
            assert not diagnostics, "child emitted a sanitized failure diagnostic"
            assert process.returncode == 0
