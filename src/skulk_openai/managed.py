"""Managed child consuming only the separately licensed public SDK interfaces."""

import argparse
import asyncio
import json
import sqlite3
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import ExitStack, aclosing
from pathlib import Path
from typing import Self

import httpx
from pydantic import Field, JsonValue, TypeAdapter, model_validator
from skulk_capability_sdk.contracts import (
    METADATA_ADMISSION,
    METADATA_ADMISSION_ANNOTATION,
    Descriptor,
    Health,
    Hello,
    Invoke,
    Manifest,
    Result,
    Shutdown,
    Startup,
    StreamInvoke,
    Surface,
    SurfaceReport,
    manifest_digest,
)
from skulk_capability_sdk.storage import read_startup
from skulk_capability_sdk.streams import (
    InlineMedia,
    StreamError,
    StreamFrame,
    execute_stream,
)
from skulk_capability_sdk.wire import ProtocolError, read_message, write_message

from skulk_inference_reference.chat import ChatCatalog, ChatEvent, GatewayProof
from skulk_inference_reference.continuation import ContinuationFailure
from skulk_inference_reference.continuation_auth import ContinuationProof, ReceiptProof
from skulk_inference_reference.continuation_wire import (
    ContinuationEvent,
    ReceiptOutcome,
    continuation_chunks,
)
from skulk_inference_reference.contracts import FrozenContract, GenerationEvent
from skulk_inference_reference.input_body import (
    BODY_MEDIA_TYPE,
    STREAMED_CHAT_CONTRACT,
    StreamedCatalog,
    StreamedProof,
)

from .admission import SignedInvocation
from .auth_contracts import AuthFailure
from .canonical import CompactionProfile
from .capability import InferenceCapability, InferenceDiscovery
from .checkpoints import CheckpointPolicy, CheckpointStore
from .continuation_service import ContinuationService
from .credentials import CredentialStore, default_state_directory
from .gateway import GatewayService
from .management import ManagementServer
from .oauth import OAuthClient
from .provider import ProviderClient, ProviderFailure, refresh_cached_catalog
from .responses import event_bytes
from .sessions import SessionManager

_OBJECT = TypeAdapter(dict[str, JsonValue])
_CONTINUATION_PROOF = TypeAdapter(ContinuationProof | ReceiptProof)
_CONTINUATION_OUTPUT = TypeAdapter(ContinuationEvent | ReceiptOutcome)


async def request_body_chunks(
    call_id: str, inputs: AsyncIterator[StreamFrame]
) -> AsyncGenerator[bytes]:
    """Yield only one complete invocation's sequenced inline request media.

    No provider URL, local path or blob reference can be resolved here. Input
    completion is required before admission; cancellation/EOF retains no body.
    """
    sequence = 0
    async for frame in inputs:
        if (
            frame.call_id != call_id
            or frame.direction != "caller_to_provider"
            or frame.sequence != sequence
        ):
            raise ValueError("invalid request input sequence")
        sequence += 1
        if frame.kind == "started" and frame.sequence == 0:
            continue
        if frame.kind == "completed" and frame.payload is None and frame.media is None:
            return
        if (
            frame.kind != "chunk"
            or frame.payload not in (None, {})
            or not isinstance(frame.media, InlineMedia)
            or frame.media.media_type != BODY_MEDIA_TYPE
        ):
            raise ValueError("invalid request media frame")
        yield frame.media.data
    raise ValueError("request input ended without completion")


def management_surfaces() -> tuple[Surface, ...]:
    """Declare the owner-local UI using the existing generic link contract."""
    return (
        Surface(surface_id="management", title="Skulk OpenAI Integrator", kind="link"),
    )


def chat_catalog_schema(*, streamed: bool = False) -> dict[str, JsonValue]:
    """Declare a strict cached catalog, or empty withdrawn availability."""
    schema = _OBJECT.validate_python(
        (StreamedCatalog if streamed else ChatCatalog).model_json_schema()
    )
    definitions = schema.pop("$defs", {})
    return {
        "$defs": definitions,
        "anyOf": [schema, {"type": "object", "additionalProperties": False}],
    }


class ContinuationConfiguration(FrozenContract):
    """Explicit owner checkpoint policy and independently qualified model profiles."""

    storage: CheckpointPolicy
    profiles: tuple[CompactionProfile, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def unique_models(self) -> Self:
        """Refuse ambiguous model defaults rather than silently choosing a profile."""
        if len({profile.model for profile in self.profiles}) != len(self.profiles):
            raise ValueError("duplicate continuation model profile")
        return self


class Configuration(FrozenContract):
    """Owner settings contain no token, grant key, callback or provider URL."""

    inference_enabled: bool = False
    state_directory: str | None = Field(default=None, min_length=1, max_length=4096)
    continuation: ContinuationConfiguration | None = Field(
        default=None,
        description=(
            "Explicit owner opt-in; absent means no checkpoint worker or storage."
        ),
    )


class Status(FrozenContract):
    """Cached management facts; no identity, credential or upstream HTTP."""

    connection: str = Field(max_length=32)
    requests_started: int = Field(ge=0)


def descriptors(*, mock: bool = False) -> tuple[Descriptor, ...]:
    """Give the synthetic and live profiles distinct immutable callable identities."""
    prefix = "mock-openai.guarded" if mock else "openai.inference"
    label = "Synthetic OpenAI" if mock else "OpenAI"
    return (
        Descriptor(
            id=prefix + ".offers",
            version="0.1.0",
            title=label + " cached offers",
            description="Cached external/unknown-usage offers; no upstream request.",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema=_OBJECT.validate_python(
                InferenceDiscovery.model_json_schema()
            ),
            annotations={METADATA_ADMISSION_ANNOTATION: METADATA_ADMISSION},
        ),
        Descriptor(
            id=prefix + ".generate",
            version="0.1.0",
            title=label + " authorized text",
            description="One signed finite admission under text.generate@0.1.0.",
            input_schema=_OBJECT.validate_python(SignedInvocation.model_json_schema()),
            output_schema={"type": "object", "additionalProperties": False},
            io_mode="server_streaming",
            output_chunk_schema=_OBJECT.validate_python(
                GenerationEvent.model_json_schema()
            ),
            annotations={
                "inference.contract": "text.generate@0.1.0",
                "inference.authorization": "reference.ed25519@0.1.0",
            },
        ),
        Descriptor(
            id=prefix + ".status",
            version="0.1.0",
            title=label + " cached status",
            description="Local connection/request-start facts; no upstream HTTP.",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema=_OBJECT.validate_python(Status.model_json_schema()),
            annotations={METADATA_ADMISSION_ANNOTATION: METADATA_ADMISSION},
        ),
        Descriptor(
            id=prefix + ".chat-catalog",
            version="0.1.0",
            title=label + " chat models",
            description="Cached owner-enabled chat models for a delegated gateway.",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema=chat_catalog_schema(),
            annotations={
                "inference.catalog": "chat.completions@0.1.0",
                METADATA_ADMISSION_ANNOTATION: METADATA_ADMISSION,
            },
        ),
        Descriptor(
            id=prefix + ".chat",
            version="0.1.0",
            title=label + " delegated chat",
            description="One signed chat request under the owner's gateway policy.",
            input_schema=_OBJECT.validate_python(GatewayProof.model_json_schema()),
            output_schema={"type": "object", "additionalProperties": False},
            io_mode="server_streaming",
            output_chunk_schema=_OBJECT.validate_python(ChatEvent.model_json_schema()),
            annotations={
                "inference.contract": "chat.completions@0.1.0",
                "inference.authorization": "gateway.ed25519@0.1.0",
            },
        ),
        Descriptor(
            id=prefix + ".chat-catalog-v2",
            version="0.2.0",
            title=label + " streamed-input chat models",
            description="Cached models for bounded signed request-body media.",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema=chat_catalog_schema(streamed=True),
            annotations={
                "inference.catalog": STREAMED_CHAT_CONTRACT,
                METADATA_ADMISSION_ANNOTATION: METADATA_ADMISSION,
            },
        ),
        Descriptor(
            id=prefix + ".chat",
            version="0.2.0",
            title=label + " delegated chat with media input",
            description="Verify a signed body received on the SDK media channel.",
            input_schema=_OBJECT.validate_python(StreamedProof.model_json_schema()),
            input_chunk_schema={"type": "object", "additionalProperties": False},
            output_schema={"type": "object", "additionalProperties": False},
            io_mode="bidirectional",
            output_chunk_schema=_OBJECT.validate_python(ChatEvent.model_json_schema()),
            annotations={
                "inference.contract": STREAMED_CHAT_CONTRACT,
                "inference.authorization": "gateway.ed25519@0.2.0",
            },
        ),
        Descriptor(
            id=prefix + ".continuation",
            version="0.1.0",
            title=label + " authenticated continuation",
            description="Signed exact-history media or content-free receipt recovery.",
            input_schema=_OBJECT.validate_python(_CONTINUATION_PROOF.json_schema()),
            input_chunk_schema={"type": "object", "additionalProperties": False},
            output_schema={"type": "object", "additionalProperties": False},
            io_mode="bidirectional",
            output_chunk_schema=_OBJECT.validate_python(
                _CONTINUATION_OUTPUT.json_schema()
            ),
            annotations={
                "inference.contract": "chat.continuation@0.1.0",
                "inference.authorization": "caller.ed25519@0.1.0",
            },
        ),
    )


def build_manifest(
    *, executable: str, executable_sha256: str, mock: bool = False
) -> Manifest:
    """Build the governed publishing contract using author SDK 0.5.3 or newer.

    The publisher replaces the placeholder artifact digest with its measured
    bytes. Account state stays outside both the manifest and generation.
    """
    return Manifest(
        bundle_id="reference.openai.mock" if mock else "reference.openai",
        bundle_version="0.4.5",
        title="Skulk OpenAI Integrator",
        skulk_requires=">=2.0.3,<3",
        executable=executable,
        executable_sha256=executable_sha256,
        arguments=(),
        descriptors=descriptors(mock=mock),
        effect="governed",
        configuration_schema=Configuration.model_json_schema(),
        runtime_dependencies=("pyjwt",),
        surfaces=management_surfaces(),
    )


def validate_manifest(manifest: Manifest) -> None:
    """Refuse schema/configuration drift and false read-only billing classification."""
    if (
        manifest.bundle_id not in {"reference.openai", "reference.openai.mock"}
        or manifest.descriptors
        != descriptors(mock=manifest.bundle_id.endswith(".mock"))
        or manifest.configuration_schema != Configuration.model_json_schema()
        or manifest.credential_inputs
        or manifest.runtime_dependencies != ("pyjwt",)
        or manifest.effect != "governed"
        or manifest.surfaces != management_surfaces()
    ):
        raise ValueError("manifest does not match the capability contract")


class MockText(httpx.AsyncByteStream):
    """Finite synthetic text with a cancellation window, never an upstream socket."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield text promptly, then allow the caller to cancel before completion."""
        item: dict[str, JsonValue] = {
            "type": "message",
            "id": "synthetic-message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": "synthetic text", "annotations": []}
            ],
        }
        yield event_bytes(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "status": "in_progress", "content": []},
            }
        )
        yield event_bytes(
            {"type": "response.output_text.delta", "delta": "synthetic text"}
        )
        await asyncio.sleep(0.2)
        yield event_bytes(
            {"type": "response.output_item.done", "output_index": 0, "item": item}
        )
        yield event_bytes(
            {
                "type": "response.completed",
                "response": {"status": "completed", "output": [item]},
            }
        )

    async def aclose(self) -> None:
        """Retain no media or external resources after iterator cleanup."""


def mock_transport() -> httpx.MockTransport:
    """Allow only finite synthetic Responses; no discovery or credential refresh."""

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url != "https://api.openai.com/v1/responses":
            return httpx.Response(503)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=MockText()
        )

    return httpx.MockTransport(respond)


async def serve(startup: Startup) -> None:
    """Own primary/media channels and all calls through disconnect and shutdown."""
    validate_manifest(startup.manifest)
    if startup.credential_config is not None:
        raise ValueError("capability owns its protected account store")
    configuration = Configuration.model_validate_json(
        json.dumps(startup.configuration or {})
    )
    # The process fence outlives every owned SDK call, including cleanup during
    # disconnect/shutdown. Never recover a journal while another child serves it.
    with ExitStack() as resources:
        await _serve(startup, configuration, resources)


async def _serve(
    startup: Startup, configuration: Configuration, resources: ExitStack
) -> None:
    mock = startup.manifest.bundle_id.endswith(".mock")
    if mock and configuration.state_directory is None:
        raise ValueError(
            "synthetic child requires an isolated explicit state directory"
        )
    root = (
        default_state_directory()
        if configuration.state_directory is None
        else await asyncio.to_thread(Path(configuration.state_directory).expanduser)
    )
    if not root.is_absolute():
        raise ValueError("managed account storage must be an absolute owner path")
    transport = mock_transport() if mock else None
    sessions = SessionManager(
        CredentialStore(root), OAuthClient(transport=transport, allow_live=not mock)
    )
    provider = ProviderClient(sessions, transport=transport, allow_live=not mock)
    published = descriptors(mock=mock)
    capability = InferenceCapability(
        provider,
        target_node=startup.transport_id,
        callable_id=published[1].id,
        descriptor_revision=published[1].revision,
        enabled=configuration.inference_enabled,
    )
    gateway = GatewayService(
        capability, callable_id=published[4].id, revision=published[4].revision
    )
    streamed_gateway = GatewayService(
        capability, callable_id=published[6].id, revision=published[6].revision
    )
    checkpoint_store: CheckpointStore | None = None
    profiles: dict[str, CompactionProfile] = {}
    worker = None
    if configuration.continuation is not None:
        policy = configuration.continuation.storage
        checkpoint_store = CheckpointStore(
            sessions.store,
            target_node=startup.transport_id,
            callable_id=published[7].id,
            descriptor_revision=published[7].revision,
            clock=capability.clock,
            retention_seconds=policy.retention_seconds,
            maximum_records=policy.maximum_records,
            maximum_storage_bytes=policy.maximum_storage_bytes,
        )
        worker = resources.enter_context(checkpoint_store.worker())
        profiles = {
            profile.model: profile for profile in configuration.continuation.profiles
        }
    reader, writer = await asyncio.open_unix_connection(startup.socket)
    executions: set[asyncio.Task[None]] = set()
    rejections: set[asyncio.Task[None]] = set()
    management = ManagementServer(capability)

    async def handler(
        call: StreamInvoke, inputs: AsyncIterator[StreamFrame]
    ) -> AsyncGenerator[StreamFrame]:
        if call.capability_id == published[7].id:
            sequence = 1
            receipt_operation = False
            try:
                proof = _CONTINUATION_PROOF.validate_json(json.dumps(call.payload))
                receipt_operation = isinstance(proof, ReceiptProof)
                if worker is None or checkpoint_store is None or not capability.enabled:
                    raise ContinuationFailure("authority_unavailable")
                if isinstance(proof, ReceiptProof):
                    async for _ in request_body_chunks(call.call_id, inputs):
                        raise ValueError("receipt operation forbids request media")
                    receipt = await asyncio.to_thread(checkpoint_store.receipt, proof)
                    yield StreamFrame(
                        call_id=call.call_id,
                        direction="provider_to_caller",
                        sequence=sequence,
                        kind="chunk",
                        payload=_OBJECT.validate_json(
                            ReceiptOutcome(receipt=receipt).model_dump_json()
                        ),
                    )
                    sequence += 1
                else:
                    service = ContinuationService(
                        worker,
                        provider,
                        profile=profiles.get(proof.request.target.model),
                    )
                    async with aclosing(
                        service.generate_streamed(
                            proof, request_body_chunks(call.call_id, inputs)
                        )
                    ) as events:
                        async for event in events:
                            for fragment in continuation_chunks(event):
                                yield StreamFrame(
                                    call_id=call.call_id,
                                    direction="provider_to_caller",
                                    sequence=sequence,
                                    kind="chunk",
                                    payload=_OBJECT.validate_json(
                                        fragment.model_dump_json()
                                    ),
                                )
                                sequence += 1
            except (ValueError, ContinuationFailure) as failure:
                code = (
                    failure.code
                    if isinstance(failure, ContinuationFailure)
                    else "authority_unavailable"
                )
                outcome = (
                    ReceiptOutcome(error=code)
                    if receipt_operation
                    else ContinuationEvent(
                        event=ChatEvent(kind="failed", error="policy_denied"),
                        continuation_error=code,
                    )
                )
                yield StreamFrame(
                    call_id=call.call_id,
                    direction="provider_to_caller",
                    sequence=sequence,
                    kind="chunk",
                    payload=_OBJECT.validate_json(outcome.model_dump_json()),
                )
                sequence += 1
            yield StreamFrame(
                call_id=call.call_id,
                direction="provider_to_caller",
                sequence=sequence,
                kind="completed",
                payload={},
            )
            return
        if call.capability_id == published[4].id:
            try:
                proof = (
                    StreamedProof.model_validate_json(json.dumps(call.payload))
                    if call.version == "0.2.0"
                    else GatewayProof.model_validate_json(json.dumps(call.payload))
                )
            except ValueError:
                yield StreamFrame(
                    call_id=call.call_id,
                    direction="provider_to_caller",
                    sequence=1,
                    kind="failed",
                    error=StreamError(code="provider_error", message="policy_denied"),
                )
                return
            sequence = 1
            output = (
                streamed_gateway.generate_streamed(
                    proof, request_body_chunks(call.call_id, inputs)
                )
                if isinstance(proof, StreamedProof)
                else gateway.generate(proof)
            )
            async with aclosing(output) as events:
                async for event in events:
                    # Carry the common terminal as a validated chunk before the
                    # transport terminal, retaining usage and finish reason.
                    yield StreamFrame(
                        call_id=call.call_id,
                        direction="provider_to_caller",
                        sequence=sequence,
                        kind="chunk",
                        payload=_OBJECT.validate_json(event.model_dump_json()),
                    )
                    sequence += 1
            yield StreamFrame(
                call_id=call.call_id,
                direction="provider_to_caller",
                sequence=sequence,
                kind="completed",
                payload={},
            )
            return
        try:
            signed = SignedInvocation.model_validate_json(json.dumps(call.payload))
        except (ValueError, TypeError):
            yield StreamFrame(
                call_id=call.call_id,
                direction="provider_to_caller",
                sequence=1,
                kind="failed",
                error=StreamError(code="provider_error", message="policy_denied"),
            )
            return
        sequence = 1
        async with aclosing(capability.generate(signed)) as events:
            async for event in events:
                if event.kind == "text":
                    yield StreamFrame(
                        call_id=call.call_id,
                        direction="provider_to_caller",
                        sequence=sequence,
                        kind="chunk",
                        payload=_OBJECT.validate_json(event.model_dump_json()),
                    )
                elif event.kind == "completed":
                    yield StreamFrame(
                        call_id=call.call_id,
                        direction="provider_to_caller",
                        sequence=sequence,
                        kind="completed",
                        payload={},
                    )
                else:
                    yield StreamFrame(
                        call_id=call.call_id,
                        direction="provider_to_caller",
                        sequence=sequence,
                        kind="failed",
                        error=StreamError(
                            code="provider_error",
                            message=event.error or "upstream_failed",
                        ),
                    )
                sequence += 1

    async def serve_one(call: StreamInvoke, *, reject: bool = False) -> None:
        try:
            await execute_stream(startup, call, overloaded if reject else handler)
        except (
            TimeoutError,
            asyncio.IncompleteReadError,
            ConnectionError,
            ProtocolError,
        ):
            return

    async def overloaded(
        call: StreamInvoke, inputs: AsyncIterator[StreamFrame]
    ) -> AsyncGenerator[StreamFrame]:
        """Refuse excess work on its own channel without killing the child."""
        _ = inputs
        yield StreamFrame(
            call_id=call.call_id,
            direction="provider_to_caller",
            sequence=1,
            kind="failed",
            error=StreamError(code="provider_error", message="overloaded"),
        )

    async def catalog_worker() -> None:
        """Own bounded account metadata refresh, never generation or grant renewal."""
        while True:
            if capability.enabled:
                await refresh_cached_catalog(provider)
            await asyncio.sleep(30)

    try:
        await management.start()
        await write_message(
            writer,
            Hello(
                host_id=startup.host_id,
                transport_node_id=startup.transport_id,
                capability_node_id=startup.node_id,
                bundle_id=startup.manifest.bundle_id,
                bundle_version=startup.manifest.bundle_version,
                token=startup.token,
                manifest_sha256=manifest_digest(startup.manifest),
            ),
        )
        async with asyncio.TaskGroup() as tasks:
            refresh = tasks.create_task(catalog_worker()) if not mock else None
            try:
                while True:
                    message = await read_message(reader)
                    if isinstance(message, Shutdown):
                        return
                    if isinstance(message, Health):
                        await write_message(
                            writer,
                            Health(
                                nonce=message.nonce,
                                ready=True,
                                surfaces=(
                                    SurfaceReport(
                                        surface_id="management",
                                        ready=True,
                                        url=management.url + "/",
                                    ),
                                ),
                            ),
                        )
                    elif isinstance(message, Invoke):
                        expected = next(
                            (
                                item
                                for item in (
                                    published[0],
                                    published[2],
                                    published[3],
                                    published[5],
                                )
                                if item.id == message.capability_id
                            ),
                            None,
                        )
                        if (
                            expected is None
                            or message.version != expected.version
                            or message.descriptor_revision != expected.revision
                            or message.payload
                        ):
                            result = Result(
                                call_id=message.call_id,
                                payload=None,
                                error="invalid_payload",
                            )
                        else:
                            if expected.id == published[5].id:
                                catalog = await streamed_gateway.streamed_catalog()
                                value = (
                                    catalog.model_dump_json()
                                    if catalog is not None
                                    else "{}"
                                )
                            elif expected.id == published[3].id:
                                catalog = await gateway.catalog()
                                value = (
                                    catalog.model_dump_json()
                                    if catalog is not None
                                    else "{}"
                                )
                            elif expected.id == published[0].id:
                                value = (await capability.discovery()).model_dump_json()
                            else:
                                value = Status(
                                    connection=(await sessions.status()).state,
                                    requests_started=provider.requests_started,
                                ).model_dump_json()
                            result = Result(
                                call_id=message.call_id,
                                payload=_OBJECT.validate_json(value),
                                error=None,
                            )
                        await write_message(writer, result)
                    elif isinstance(message, StreamInvoke):
                        expected = next(
                            (
                                item
                                for item in (
                                    published[1],
                                    published[4],
                                    published[6],
                                    published[7],
                                )
                                if item.id == message.capability_id
                                and item.version == message.version
                            ),
                            None,
                        )
                        if (
                            expected is None
                            or message.version != expected.version
                            or message.descriptor_revision != expected.revision
                        ):
                            raise ProtocolError("invalid managed stream")
                        if len(executions) >= 4:
                            if len(rejections) < 4:
                                rejection = tasks.create_task(
                                    serve_one(message, reject=True)
                                )
                                rejections.add(rejection)
                                rejection.add_done_callback(rejections.discard)
                            # Further excess calls expire at their owner deadline;
                            # never allocate unbounded rejection tasks or block
                            # primary health/shutdown on a media handshake.
                        else:
                            execution = tasks.create_task(serve_one(message))
                            executions.add(execution)
                            execution.add_done_callback(executions.discard)
                    else:
                        raise ProtocolError("unexpected primary message")
            finally:
                for execution in (*executions, *rejections):
                    execution.cancel()
                if refresh is not None:
                    refresh.cancel()
    finally:
        await management.close()
        writer.close()
        await writer.wait_closed()


def main() -> None:
    """Read owner FD startup only; diagnostics never include caller or token data."""
    parser = argparse.ArgumentParser(description="Managed OpenAI inference capability")
    parser.add_argument("--config-fd", required=True, type=int)
    arguments = parser.parse_args()
    try:
        asyncio.run(serve(read_startup(arguments.config_fd)))
    except (
        AuthFailure,
        ProviderFailure,
        ContinuationFailure,
        OSError,
        ValueError,
        sqlite3.Error,
        ProtocolError,
        ExceptionGroup,
    ):
        print("managed inference capability failed", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
