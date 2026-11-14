"""Native managed entrypoint consuming only public capability SDK interfaces.

Host composition authority and unchanged application proof authority are both
required. Provider sessions, UI, policy and checkpoint custody stay in this app.
"""

import argparse
import asyncio
import json
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
from .state_transfer import read_native_adoption

_OBJECT = TypeAdapter(dict[str, JsonValue])
_TEXT = TypeAdapter(str)


class NativeConfiguration(FrozenContract):
    """Explicit legacy proof identity and validated application settings."""

    legacy_target_node: Identifier = Field(
        description="Unchanged application proof target; never a native revision alias."
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
    async for frame in inputs:
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
    services: ApplicationServices | None = None

    @asynccontextmanager
    async def setup(startup: ApplicationStartup) -> AsyncGenerator[None]:
        nonlocal services
        configuration = NativeConfiguration.model_validate_json(
            json.dumps(startup.configuration)
        )
        # Adoption must name its store explicitly. A packaging rehearsal must
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
        # No account store is initialized/recovered until the explicit offline
        # receipt binds this actual child and both independent contract families.
        receipt = await asyncio.to_thread(read_native_adoption, root)
        if (
            receipt.native_node_id != startup.node_id
            or receipt.legacy_target_node != configuration.legacy_target_node
            or receipt.native_bundle_id
            != ("reference.openai.native.mock" if mock else "reference.openai.native")
            or receipt.bindings != adoption_bindings(mock=mock)
        ):
            raise ValueError("native application adoption receipt differs")
        async with open_application(
            configuration.legacy_target_node, configuration.application, mock=mock
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
