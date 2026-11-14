"""Explicit native envelopes around unchanged application proof contracts.

A native descriptor has its own full identity. Legacy signed proofs and stored
checkpoint targets retain their original sixteen-character revision. Neither
identity is padded, truncated or asserted to mean the other.
"""

import importlib
import json
from dataclasses import dataclass
from typing import Protocol, cast

from pydantic import JsonValue, TypeAdapter
from skulk_capability_sdk.contracts import Descriptor

from .managed import descriptors
from .state_transfer import AdoptionBinding

_OBJECT = TypeAdapter(dict[str, JsonValue])


class NativeRecord(Protocol):
    """Public compiled record projection, without host implementation imports."""

    @property
    def digest(self) -> str:
        """Return the actual full record identity, never a legacy revision."""
        ...

    def admit_hello(self, source: str, call_id: str | None = None) -> "NativeRecord":
        """Match the startup/call hello; the host separately owns one-use custody."""
        ...

    def canonical(self) -> bytes:
        """Return the actual compiled validator's canonical declaration."""
        ...


class RecordFactory(Protocol):
    """The public native ABI's record constructor."""

    def from_json(self, name: str, source: str) -> NativeRecord:
        """Admit a declaration using the actual immutable native kernel."""
        ...


class NativeKernel(Protocol):
    """Only the public author declaration and profile APIs used by this app."""

    ManagedV2Contract: RecordFactory

    def portable_v2_profile_digest(self) -> str:
        """Return the actual portable/2 profile identity."""
        ...

    def managed_v2_profile_digest(self) -> str:
        """Return the actual managed/2 profile identity."""
        ...

    def portable_v2_contract_digest(self, name: str, source: str) -> str:
        """Admit and identify a native author declaration."""
        ...


def public_kernel() -> NativeKernel:
    """Load only the explicit public compiled ABI, never a replacement validator."""
    return cast(
        NativeKernel, cast(object, importlib.import_module("_skulk_capability_kernel"))
    )


@dataclass(frozen=True)
class NativeBinding:
    """An explicit pair of independent native and legacy application identities."""

    legacy: Descriptor
    declaration: dict[str, JsonValue]
    revision: str


def envelope_schema(revision: str) -> dict[str, JsonValue]:
    """Declare a versioned envelope; domain models still validate its inner payload.

    Arbitrary inner JSON is intentional here. It is not a weakened re-export of
    the legacy schema, whose Unicode identifier rules exceed portable/2 regex.
    The unchanged strict legacy model is separately enforced by app behavior.
    """
    return {
        "type": "object",
        "properties": {
            "contract": {"const": "openai.legacy-envelope@1.0.0"},
            "legacy_revision": {"const": revision},
            "payload": {"type": "object"},
        },
        "required": ["contract", "legacy_revision", "payload"],
        "additionalProperties": False,
    }


def native_bindings(*, mock: bool = False) -> tuple[NativeBinding, ...]:
    """Bind every actual legacy callable to a distinct portable native declaration."""
    kernel = public_kernel()
    values: list[NativeBinding] = []
    for legacy in descriptors(mock=mock):
        inference = legacy.io_mode != "unary"
        envelope = envelope_schema(legacy.revision)
        declaration: dict[str, JsonValue] = {
            "contract_profile": "skulk.portable/2",
            "profile_digest": kernel.portable_v2_profile_digest(),
            "id": legacy.id.replace(".inference.", ".native.").replace(
                ".guarded.", ".native."
            ),
            "version": legacy.version,
            "title": legacy.title,
            "description": "Native envelope for " + legacy.description,
            "behavior": "inference" if inference else "read",
            "processing_domain": "external" if inference else "local",
            "io_mode": legacy.io_mode,
            "input_schema": envelope,
            "output_schema": envelope
            if not inference
            else {"type": "object", "additionalProperties": False},
            "input_chunk_schema": {"type": "object", "additionalProperties": False}
            if legacy.input_chunk_schema is not None
            else None,
            "output_chunk_schema": envelope
            if legacy.output_chunk_schema is not None
            else None,
            "annotations": {
                "inference.contract": "openai.legacy-envelope@1.0.0",
                "openai.legacy-id": legacy.id,
                "openai.legacy-version": legacy.version,
                "openai.legacy-revision": legacy.revision,
            },
        }
        revision = kernel.portable_v2_contract_digest(
            "Descriptor", json.dumps(declaration)
        )
        values.append(NativeBinding(legacy, declaration, revision))
    return tuple(values)


def adoption_bindings(*, mock: bool = False) -> tuple[AdoptionBinding, ...]:
    """Expose exact independently computed pairs for an offline adoption receipt."""
    return tuple(
        AdoptionBinding(
            legacy_id=binding.legacy.id,
            version=binding.legacy.version,
            legacy_revision=binding.legacy.revision,
            native_id=TypeAdapter(str).validate_python(
                binding.declaration["id"], strict=True
            ),
            native_revision=binding.revision,
        )
        for binding in native_bindings(mock=mock)
    )


def wrap_payload(
    binding: NativeBinding, payload: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    """Return the declared native envelope without modifying the legacy payload."""
    return {
        "contract": "openai.legacy-envelope@1.0.0",
        "legacy_revision": binding.legacy.revision,
        "payload": payload,
    }


def unwrap_payload(
    binding: NativeBinding, payload: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    """Refuse cross-contract aliases before entering strict legacy domain behavior."""
    if (
        set(payload) != {"contract", "legacy_revision", "payload"}
        or payload["contract"] != "openai.legacy-envelope@1.0.0"
        or payload["legacy_revision"] != binding.legacy.revision
    ):
        raise ValueError("native application contract binding differs")
    return _OBJECT.validate_python(payload["payload"], strict=True)


def build_native_manifest(
    *, executable: str, executable_sha256: str, mock: bool = False
) -> dict[str, JsonValue]:
    """Build and admit the native app declaration; grant no authority."""
    kernel = public_kernel()
    configuration: dict[str, JsonValue] = {
        "type": "object",
        "properties": {
            "legacy_target_node": {"type": "string", "minLength": 1, "maxLength": 128},
            "application": {"type": "object"},
        },
        "required": ["legacy_target_node", "application"],
        "additionalProperties": False,
    }
    declarations = TypeAdapter(list[JsonValue]).validate_json(
        json.dumps([b.declaration for b in native_bindings(mock=mock)])
    )
    value: dict[str, JsonValue] = {
        "protocol": 6,
        "contract_profile": "skulk.managed/2",
        "profile_digest": kernel.managed_v2_profile_digest(),
        "bundle_id": "reference.openai.native.mock"
        if mock
        else "reference.openai.native",
        "bundle_version": "0.6.0",
        "title": "Skulk OpenAI Integrator",
        "skulk_requires": ">=2.0.3,<3",
        "executable": executable,
        "executable_sha256": executable_sha256,
        "arguments": ["--mock"] if mock else [],
        "effect": "inference",
        "contracts": {
            "protocol": 6,
            "contract_profile": "skulk.portable/2",
            "profile_digest": kernel.portable_v2_profile_digest(),
            "configuration_schema": configuration,
            "descriptors": declarations,
        },
        "runtime_dependencies": [
            "pyjwt",
            "skulk-capability-kernel",
            "skulk-capability-sdk",
        ],
        "bounds": {
            "startup_seconds": 30,
            "health_seconds": 3,
            "call_seconds": 10,
            "stream_seconds": 300,
            "shutdown_seconds": 5,
            "backoff_seconds": 1,
            "restart_budget": 3,
            "max_children": 4,
            "log_bytes": 0,
            "concurrency": 4,
            "inference_seconds": None,
        },
        "credential_inputs": [],
        "operations": None,
        "preflight_profile": None,
        "steward": [],
        "surfaces": TypeAdapter(list[JsonValue]).validate_json(
            json.dumps(
                [
                    {
                        "surface_id": "management",
                        "title": "Skulk OpenAI Integrator",
                        "kind": "proxied",
                        "entry": "/",
                        "websocket": True,
                    }
                ]
            )
        ),
    }
    return _OBJECT.validate_json(
        kernel.ManagedV2Contract.from_json("Manifest", json.dumps(value)).canonical()
    )
