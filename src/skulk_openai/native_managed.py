"""Native managed entrypoint consuming only public capability SDK interfaces.

Host composition authority and unchanged application proof authority are both
required. Provider sessions, UI, policy and checkpoint custody stay in this app.
"""

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, JsonValue, TypeAdapter
from skulk_capability_sdk.application import (
    ApplicationStartup,
    CapabilityApp,
    InputFrame,
    Invocation,
    OutputFrame,
)
from skulk_capability_sdk.streams import InlineMedia, StreamFrame

from skulk_inference_reference.continuation import ContinuationFailure
from skulk_inference_reference.contracts import FrozenContract, Identifier
from skulk_inference_reference.input_body import MAX_BODY_BYTES, MAX_BODY_CHUNK_BYTES

from .auth_contracts import AuthFailure
from .managed import (
    ApplicationCall,
    ApplicationServices,
    Configuration,
    open_application,
)
from .native_contracts import (
    NativeBinding,
    adoption_bindings,
    native_bindings,
    unwrap_payload,
    wrap_payload,
)
from .provider import ProviderFailure
from .state_transfer import native_proof_target

_OBJECT = TypeAdapter(dict[str, JsonValue])
_TEXT = TypeAdapter(str)


class NativeConfiguration(FrozenContract):
    """Validated state settings and optional existing-state proof identity."""

    legacy_target_node: Identifier | None = Field(
        default=None,
        description="Original target for an explicit state handoff.",
    )
    application: Configuration


def bound_call(call: Invocation, bindings: tuple[NativeBinding, ...]) -> NativeBinding:
    """Require this code's exact descriptor/domain in addition to SDK host admission."""
    binding = next(
        (
            item
            for item in bindings
            if item.declaration["id"] == call.capability_id
            and item.legacy.version == call.version
        ),
        None,
    )
    if binding is None:
        raise ValueError("native application target differs")
    target = _OBJECT.validate_python(call.context.export()["executor"], strict=True)
    if (
        target["descriptor_revision"] != binding.revision
        or target["behavior"] != binding.declaration["behavior"]
        or target["processing_domain"] != binding.declaration["processing_domain"]
    ):
        raise ValueError("native application authority binding differs")
    return binding


async def application_inputs(
    inputs: AsyncIterator[InputFrame],
) -> AsyncGenerator[StreamFrame]:
    """Project admitted inline media into domain frames without blob resolution."""
    received_bytes = 0
    async for frame in inputs:
        received_bytes += len(frame.media)
        if len(frame.media) > MAX_BODY_CHUNK_BYTES or received_bytes > MAX_BODY_BYTES:
            raise ValueError("native request media exceeds application bounds")
        projection = frame.projection
        metadata = projection.get("media")
        media = None
        if metadata is not None:
            fields = _OBJECT.validate_python(metadata, strict=True)
            if fields["kind"] != "inline":
                raise ValueError("application accepts inline input only")
            media = InlineMedia.model_validate(
                {**fields, "data": frame.media}, strict=True
            )
        yield StreamFrame.model_validate({**projection, "media": media}, strict=True)


def create_app(*, mock: bool = False) -> CapabilityApp:
    """Create explicit native handlers; no account or state is opened before setup."""
    if version("skulk-capability-kernel") != "0.7.0rc4":
        raise ValueError("native Integrator requires its explicit stream cohort")
    bindings = native_bindings(mock=mock)
    chat = next(
        binding
        for binding in bindings
        if (binding.legacy.annotations or {}).get("inference.contract")
        == "chat.completions@0.2.0"
    )
    chat_target = (
        _TEXT.validate_python(chat.declaration["id"], strict=True)
        + "@"
        + chat.legacy.version
    )
    services: ApplicationServices | None = None

    def matches_model_address(address: str, model: str) -> bool:
        # Core owns the installation namespace and independently pins actual
        # native custody. The app validates its declared chat target and signed
        # model slug; the installation label never confers account authority.
        parts = address.split("/")
        return (
            len(parts) == 4
            and parts[0] == "native"
            and 1 <= len(parts[1]) <= 128
            and re.fullmatch(r"[A-Za-z0-9_.:-]+", parts[1]) is not None
            and parts[2] == chat_target
            and parts[3] == model
        )

    @asynccontextmanager
    async def setup(startup: ApplicationStartup) -> AsyncGenerator[None]:
        nonlocal services
        configuration = NativeConfiguration.model_validate_json(
            json.dumps(startup.configuration)
        )
        # Setup must name its store explicitly. A packaging rehearsal must
        # never accidentally initialize or recover the operator's default store.
        if configuration.application.state_directory is None:
            raise ValueError(
                "native application needs an explicit protected state root"
            )
        root = await asyncio.to_thread(
            Path(configuration.application.state_directory).expanduser
        )
        if not root.is_absolute():
            raise ValueError("native application state root must be absolute")
        target = await asyncio.to_thread(
            native_proof_target,
            root,
            node_id=startup.node_id,
            bundle_id="reference.openai.native.mock"
            if mock
            else "reference.openai.native",
            bindings=adoption_bindings(mock=mock),
            legacy_target_node=configuration.legacy_target_node,
        )
        async with open_application(
            target,
            configuration.application,
            mock=mock,
            chat_model_address_matches=matches_model_address,
        ) as active:
            services = active
            try:
                yield
            finally:
                services = None

    def surfaces() -> list[dict[str, JsonValue]]:
        if services is None:
            return []
        return [
            {
                "surface_id": "management",
                "ready": True,
                "url": None,
                "port": urlsplit(services.management.url).port,
            }
        ]

    async def read(call: Invocation) -> dict[str, JsonValue]:
        binding = bound_call(call, bindings)
        if services is None or unwrap_payload(binding, call.payload):
            raise ValueError("native read input refused")
        return wrap_payload(binding, await services.read(binding.legacy.id))

    async def stream(
        call: Invocation, inputs: AsyncIterator[InputFrame]
    ) -> AsyncGenerator[OutputFrame]:
        binding = bound_call(call, bindings)
        if services is None:
            raise ValueError("native application is not ready")
        legacy = ApplicationCall(
            call.call_id,
            binding.legacy.id,
            binding.legacy.version,
            unwrap_payload(binding, call.payload),
        )
        async with aclosing(application_inputs(inputs)) as input_frames:
            async with aclosing(services.stream(legacy, input_frames)) as events:
                async for event in events:
                    if event.kind == "started":
                        raise ValueError("domain output cannot restart a direction")
                    if binding.legacy.io_mode == "bidirectional" and event.kind in {
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        # An authentication refusal can precede the input opener.
                        # Keep the duplex socket alive for the caller's bounded
                        # upload before publishing its transport terminal. Closing
                        # it early races the host write and erases the refusal.
                        async with asyncio.timeout(30):
                            async for remainder in input_frames:
                                if remainder.kind in {"failed", "cancelled"}:
                                    raise ValueError(
                                        "native caller input did not finish"
                                    )
                    yield OutputFrame(
                        event.kind,
                        wrap_payload(binding, event.payload)
                        if event.kind == "chunk" and event.payload is not None
                        else event.payload,
                        error=event.error.code if event.error is not None else None,
                    )

    app = CapabilityApp(
        setup=setup, ready=lambda: services is not None, surfaces=surfaces
    )
    for binding in bindings:
        identifier = _TEXT.validate_python(binding.declaration["id"], strict=True)
        if binding.legacy.io_mode == "unary":
            app.unary(identifier, read, version=binding.legacy.version)
        else:
            app.stream(identifier, stream, version=binding.legacy.version)
    return app


def main() -> None:
    """Consume only inherited startup material; print no provider or caller details."""
    parser = argparse.ArgumentParser(description="Native Skulk OpenAI Integrator")
    parser.add_argument("--config-fd", required=True, type=int)
    parser.add_argument("--mock", action="store_true")
    arguments = parser.parse_args()
    try:
        asyncio.run(create_app(mock=arguments.mock).serve(arguments.config_fd))
    except (
        AuthFailure,
        ProviderFailure,
        ContinuationFailure,
        OSError,
        ValueError,
        sqlite3.Error,
        ExceptionGroup,
    ):
        print("native managed capability failed", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
