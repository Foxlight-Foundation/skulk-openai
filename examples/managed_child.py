"""Credential-free managed child using only public capability SDK interfaces.

Run through a compatible managed owner or the explicit protocol tests. This
example is intentionally not a signed installed provider or real authorization.
"""

import argparse
import asyncio
import json
import sys
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import Literal

from pydantic import Field, JsonValue, TypeAdapter
from skulk_capability_sdk.contracts import (
    Descriptor,
    Health,
    Hello,
    Invoke,
    Result,
    Shutdown,
    Startup,
    StreamInvoke,
    manifest_digest,
)
from skulk_capability_sdk.storage import read_startup
from skulk_capability_sdk.streams import StreamError, StreamFrame, execute_stream
from skulk_capability_sdk.wire import ProtocolError, read_message, write_message

from skulk_inference_reference.contracts import (
    Authority,
    FrozenContract,
    GenerationEvent,
    GenerationRequest,
    Offer,
    Requirements,
)
from skulk_inference_reference.dispatch import admitted_stream
from skulk_inference_reference.fixtures import (
    fixture_authority,
    fixture_generate,
    fixture_offer,
)
from skulk_openai.responses import MockResponsesAdapter, text_transport

_OBJECT = TypeAdapter(dict[str, JsonValue])


class Configuration(FrozenContract):
    """Non-secret synthetic profile selection; no credential field is supported."""

    profile: Literal["fixture", "mock-openai"] = "fixture"
    enabled: bool = True
    delay_seconds: float = Field(default=0.0, ge=0, le=1)


def descriptors(profile: Literal["fixture", "mock-openai"]) -> tuple[Descriptor, ...]:
    """Describe distinct callable IDs with identical common text semantics."""
    return (
        Descriptor(
            id=f"{profile}.inference.offers",
            version="0.1.0",
            title="Mock offers",
            description="Synthetic offers only; no account eligibility or authority.",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={
                "type": "object",
                "properties": {
                    "offers": {
                        "type": "array",
                        "maxItems": 1,
                        "items": Offer.model_json_schema(),
                    }
                },
                "required": ["offers"],
                "additionalProperties": False,
            },
        ),
        Descriptor(
            id=f"{profile}.inference.generate",
            version="0.1.0",
            title="Mock text",
            description="Synthetic text under the shared inference contract.",
            input_schema=_OBJECT.validate_python(GenerationRequest.model_json_schema()),
            output_schema={"type": "object", "additionalProperties": False},
            io_mode="server_streaming",
            output_chunk_schema=_OBJECT.validate_python(
                GenerationEvent.model_json_schema()
            ),
            annotations={"inference.contract": "text.generate@0.1.0"},
        ),
    )


def advertised_offer(configuration: Configuration) -> Offer:
    """Bind the synthetic offer to its actual callable descriptor revision."""
    original = fixture_offer(external=configuration.profile == "mock-openai")
    return Offer.model_validate(
        {
            **original.model_dump(),
            "ready": configuration.enabled,
            "expires_at": time.time() + 300.0,
            "descriptor_revision": descriptors(configuration.profile)[1].revision,
        }
    )


async def serve(startup: Startup) -> None:
    """Authenticate startup and own all stream tasks through primary disconnect.

    Health remains responsive while the stream helper owns output/backpressure.
    Stream errors fail the process; they are not detached background exceptions.
    """
    if startup.credential_config is not None:
        raise ValueError("this mock child must not receive a credential backend")
    configuration = Configuration.model_validate(startup.configuration or {})
    if startup.manifest.descriptors != descriptors(configuration.profile):
        raise ValueError("manifest does not match this synthetic profile")
    reader, writer = await asyncio.open_unix_connection(startup.socket)
    execution: asyncio.Task[None] | None = None
    offer = advertised_offer(configuration)
    authority = Authority.model_validate(
        {
            **fixture_authority().model_dump(),
            "expires_at": offer.expires_at,
        }
    )

    async def handler(
        call: StreamInvoke,
        inputs: AsyncIterator[StreamFrame],
    ) -> AsyncGenerator[StreamFrame]:
        _ = inputs
        try:
            request = GenerationRequest.model_validate_json(json.dumps(call.payload))
        except ValueError:
            yield StreamFrame(
                call_id=call.call_id,
                direction="provider_to_caller",
                sequence=1,
                kind="failed",
                error=StreamError(code="provider_error", message="invalid_payload"),
            )
            return
        requirements = Requirements(
            allowed_domains=frozenset({"local", "external"}),
            allowed_usage=frozenset({"none", "included_only"}),
        )

        def generate(item: GenerationRequest) -> AsyncGenerator[GenerationEvent]:
            if configuration.profile == "fixture":
                return fixture_generate(item)
            return MockResponsesAdapter(text_transport()).generate(
                item, model="synthetic-text"
            )

        stream = admitted_stream(
            generate,
            request,
            requirements,
            lambda: offer,
            lambda: authority,
            now=time.time,
        )
        sequence = 1
        async with aclosing(stream) as output:
            async for event in output:
                await asyncio.sleep(configuration.delay_seconds)
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
                            message=event.error or "provider_error",
                        ),
                    )
                sequence += 1

    async def serve_one(call: StreamInvoke) -> None:
        try:
            await execute_stream(startup, call, handler)
        except (
            TimeoutError,
            asyncio.IncompleteReadError,
            ConnectionError,
            ProtocolError,
        ):
            # The helper has already closed its owned iterator/socket. The owner
            # observes media EOF; a cancelled call must not kill later health.
            return

    try:
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
            try:
                while True:
                    message = await read_message(reader)
                    if isinstance(message, Shutdown):
                        return
                    if isinstance(message, Health):
                        await write_message(
                            writer, Health(nonce=message.nonce, ready=True)
                        )
                    elif isinstance(message, Invoke):
                        expected = descriptors(configuration.profile)[0]
                        if (
                            message.capability_id != expected.id
                            or message.version != expected.version
                            or message.descriptor_revision != expected.revision
                            or message.payload
                        ):
                            await write_message(
                                writer,
                                Result(
                                    call_id=message.call_id,
                                    payload=None,
                                    error="invalid_payload",
                                ),
                            )
                        else:
                            offers = (
                                [offer.model_dump(mode="json")]
                                if configuration.enabled
                                and offer.expires_at > time.time()
                                else []
                            )
                            await write_message(
                                writer,
                                Result(
                                    call_id=message.call_id,
                                    payload=_OBJECT.validate_python({"offers": offers}),
                                    error=None,
                                ),
                            )
                    elif isinstance(message, StreamInvoke):
                        expected = descriptors(configuration.profile)[1]
                        if (
                            message.capability_id != expected.id
                            or message.version != expected.version
                            or message.descriptor_revision != expected.revision
                            or (execution is not None and not execution.done())
                        ):
                            raise ProtocolError("invalid or overlapping managed call")
                        execution = tasks.create_task(serve_one(message))
                    else:
                        raise ProtocolError("unexpected primary message")
            finally:
                if execution is not None:
                    execution.cancel()
    finally:
        writer.close()
        await writer.wait_closed()


def main() -> None:
    """Read owner-provisioned startup data from an inherited file descriptor."""
    parser = argparse.ArgumentParser(description="Credential-free managed mock child")
    parser.add_argument("--config-fd", required=True, type=int)
    arguments = parser.parse_args()
    try:
        asyncio.run(serve(read_startup(arguments.config_fd)))
    except (OSError, ValueError, ProtocolError, ExceptionGroup):
        # Pydantic exceptions can include input; the process boundary logs only
        # the failure class's generic outcome, never startup or call payloads.
        print("managed mock child failed", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
