"""Provider-neutral experimental gateway/chat contract, independent of OpenAI.

The gateway authenticates its API caller before signing. A provider must also
hold an explicit owner delegation for that gateway key; fabric membership and
the public key carried in this envelope confer no authority by themselves.
"""

import json
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .contracts import FrozenContract, Identifier

CHAT_CONTRACT = "chat.completions@0.1.0"


class ChatModel(FrozenContract):
    """A cached, owner-enabled model addressable through a gateway."""

    id: Identifier
    name: str = Field(min_length=1, max_length=256)
    features: tuple[
        Literal["image_input", "function_tools", "reasoning_toggle"], ...
    ] = Field(default=(), max_length=3)


class ChatCatalog(FrozenContract):
    """Account-free routing facts pinned to a provider's current local policy."""

    contract: Literal["chat.completions@0.1.0"] = CHAT_CONTRACT
    target_node: Identifier
    callable_id: Identifier
    callable_version: Literal["0.1.0"] = "0.1.0"
    descriptor_revision: str = Field(pattern=r"^[a-f0-9]{16}$")
    catalog_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: float = Field(gt=0)
    models: tuple[ChatModel, ...] = Field(max_length=128)
    processing_domain: Literal["local", "private_site", "external"]
    usage: Literal["none", "included_only", "credits_possible", "unknown"]
    usage_label: str = Field(max_length=80)
    usage_url: str | None = Field(default=None, max_length=256)


class GatewayInvocation(FrozenContract):
    """Exact target and complete chat request, signed for one short-lived use."""

    contract: Literal["chat.completions@0.1.0"] = CHAT_CONTRACT
    public_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_node: Identifier
    callable_id: Identifier
    descriptor_revision: str = Field(pattern=r"^[a-f0-9]{16}$")
    catalog_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    model: Identifier
    request: dict[str, JsonValue]
    nonce: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: float = Field(gt=0)

    @model_validator(mode="after")
    def bounded(self) -> "GatewayInvocation":
        """Keep signed control messages beneath the SDK's bounded wire size."""
        if len(self.canonical()) > 49_152:
            raise ValueError("chat request exceeds gateway byte bound")
        return self

    def canonical(self) -> bytes:
        """Encode identically across provider-independent gateway implementations."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()


class GatewayProof(FrozenContract):
    """One-use signed request; contains no upstream or gateway private key."""

    invocation: GatewayInvocation
    signature: str = Field(pattern=r"^[a-f0-9]{128}$")


class ChatEvent(FrozenContract):
    """A Chat Completions delta, terminal finish reason, or safe fixed error."""

    kind: Literal["delta", "completed", "failed"]
    delta: dict[str, JsonValue] = Field(default_factory=dict)
    finish_reason: Literal["stop", "tool_calls"] | None = None
    usage: dict[str, int] | None = None
    error: (
        Literal[
            "policy_denied",
            "invalid_request",
            "upstream_failed",
            "incomplete",
            "interrupted",
            "invalid_stream",
            "output_limit",
            "usage_limit",
            "usage_unavailable",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def shape(self) -> "ChatEvent":
        """Prevent a failure or terminal from masquerading as generated content."""
        if self.kind == "delta":
            if not self.delta or self.error or self.finish_reason or self.usage:
                raise ValueError("invalid delta")
        elif self.kind == "completed":
            if self.delta or self.error or self.finish_reason is None:
                raise ValueError("invalid completion")
        elif self.delta or self.finish_reason or self.usage or self.error is None:
            raise ValueError("invalid failure")
        return self
