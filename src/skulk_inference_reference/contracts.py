"""Strict, bounded draft text-generation semantics shared by reference providers."""

import json
from typing import Annotated, Literal, Self, final

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[\w.:@-]+$")]
ProcessingDomain = Literal["local", "private_site", "external"]
UsageClass = Literal["none", "included_only", "credits_possible", "unknown"]
CONTRACT = "text.generate@0.1.0"
MAX_REQUEST_BYTES = 32_768


class FrozenContract(BaseModel):
    """Reject coercion, extra fields and nonfinite numbers at ownership boundaries."""

    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", allow_inf_nan=False
    )


@final
class Message(FrozenContract):
    """A bounded text message; system roles and arbitrary tools are not supported."""

    role: Literal["developer", "user", "assistant"]
    content: str = Field(min_length=1, max_length=4096)


@final
class GenerationRequest(FrozenContract):
    """Untrusted text input, with a local output bound rather than a billing cap."""

    messages: tuple[Message, ...] = Field(min_length=1, max_length=16)
    maximum_output_characters: int = Field(default=1024, ge=1, le=4096)

    @model_validator(mode="after")
    def bounded_encoding(self) -> Self:
        """Reserve room beneath the managed control-envelope ceiling."""
        # Match the SDK control encoder: astral characters expand to surrogate
        # escapes with ensure_ascii=True, so UTF-8 size alone is insufficient.
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("request exceeds reference byte bound")
        return self


@final
class Requirements(FrozenContract):
    """Hard caller constraints plus an ordered soft provider preference."""

    allowed_domains: frozenset[ProcessingDomain] = frozenset({"local"})
    allowed_usage: frozenset[UsageClass] = frozenset({"none"})
    minimum_context_tokens: int = Field(default=1, ge=1, le=1_000_000)
    preferred_providers: tuple[Identifier, ...] = Field(default=(), max_length=16)


@final
class Offer(FrozenContract):
    """A fresh model offer; account identity and credentials never belong here."""

    contract: str = Field(default=CONTRACT, max_length=128)
    offer_id: Identifier
    provider: Identifier
    model: Identifier
    callable_id: Identifier
    callable_version: Literal["0.1.0"] = "0.1.0"
    descriptor_revision: str = Field(pattern=r"^[a-f0-9]{16}$")
    offer_revision: int = Field(ge=1)
    domain: ProcessingDomain
    usage: UsageClass
    context_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    ready: bool
    expires_at: float = Field(ge=0)


@final
class Authority(FrozenContract):
    """Synthetic host-supplied grant; never deserialize this from inference input.

    This reference does not authenticate principals or implement production grants.
    Its trusted in-process owner supplies and can replace this snapshot.
    """

    principal: Identifier
    revision: int = Field(ge=1)
    allowed_offers: frozenset[Identifier]
    allowed_domains: frozenset[ProcessingDomain]
    allowed_usage: frozenset[UsageClass]
    maximum_output_characters: int = Field(ge=1, le=4096)
    expires_at: float = Field(ge=0)


RejectionReason = Literal[
    "contract_mismatch",
    "not_ready",
    "expired_offer",
    "expired_authority",
    "offer_forbidden",
    "domain_forbidden",
    "usage_forbidden",
    "context_unknown",
    "context_insufficient",
    "output_forbidden",
]


@final
class Rejection(FrozenContract):
    """A content-free explanation for excluding one offer."""

    offer_id: Identifier
    reasons: tuple[RejectionReason, ...]


@final
class Selection(FrozenContract):
    """An exact draft target and policy revision to recheck before dispatch."""

    offer: Offer
    principal: Identifier
    authority_revision: int
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    requirements: Requirements
    rejected: tuple[Rejection, ...]


@final
class GenerationEvent(FrozenContract):
    """One common text delta or explicit terminal; never an upstream error body."""

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        allow_inf_nan=False,
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "kind": {"const": "text"},
                        "text": {"type": "string", "minLength": 1},
                        "error": {"type": "null"},
                    },
                    "required": ["text"],
                },
                {
                    "properties": {
                        "kind": {"const": "completed"},
                        "text": {"type": "null"},
                        "error": {"type": "null"},
                    }
                },
                {
                    "properties": {
                        "kind": {"const": "failed"},
                        "text": {"type": "null"},
                        "error": {"type": "string"},
                    },
                    "required": ["error"],
                },
            ]
        },
    )

    kind: Literal["text", "completed", "failed"]
    text: str | None = Field(default=None, max_length=4096)
    error: (
        Literal[
            "upstream_failed",
            "incomplete",
            "interrupted",
            "invalid_stream",
            "output_limit",
            "policy_denied",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def valid_shape(self) -> Self:
        """Keep terminal outcomes unambiguous and prevent failure payload retention."""
        if self.kind == "text":
            if not self.text or self.error is not None:
                raise ValueError("text delta requires only text")
        elif self.kind == "completed":
            if self.text is not None or self.error is not None:
                raise ValueError("completed carries no content")
        elif self.text is not None or self.error is None:
            raise ValueError("failure requires only a bounded error code")
        return self
