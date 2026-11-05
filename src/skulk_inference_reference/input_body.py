"""Provider-neutral signed body references for bounded managed media transfer.

The control message authenticates a digest and length. Request bytes travel on
the invocation's existing SDK media channel, never in discovery or shared state.
"""

import hashlib
import json
from collections.abc import AsyncIterable
from typing import Literal

from pydantic import Field, JsonValue, TypeAdapter

from .contracts import FrozenContract, Identifier

MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_BODY_CHUNK_BYTES = 1024 * 1024
BODY_MEDIA_TYPE = "application/vnd.skulk.chat-request+json"
STREAMED_CHAT_CONTRACT = "chat.completions@0.2.0"
_OBJECT = TypeAdapter(dict[str, JsonValue])


class InputBody(FrozenContract):
    """Exact UTF-8 JSON bytes authorized for one invocation, not a fetchable URL."""

    size_bytes: int = Field(ge=2, le=MAX_BODY_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: Literal["application/vnd.skulk.chat-request+json"] = BODY_MEDIA_TYPE

    @classmethod
    def from_bytes(cls, raw: bytes) -> "InputBody":
        """Describe bytes without retaining their contents in the control plane."""
        return cls(size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


class StreamedInvocation(FrozenContract):
    """Sign the same target and policy facts as v1, with a body reference."""

    contract: Literal["chat.completions@0.2.0"] = STREAMED_CHAT_CONTRACT
    public_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_node: Identifier
    callable_id: Identifier
    descriptor_revision: str = Field(pattern=r"^[a-f0-9]{16}$")
    catalog_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    model: Identifier
    body: InputBody
    nonce: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: float = Field(gt=0)

    def canonical(self) -> bytes:
        """Use v1's canonical signing rules without including request contents."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()


class StreamedProof(FrozenContract):
    """One-use gateway authority over an exact body and destination."""

    invocation: StreamedInvocation
    signature: str = Field(pattern=r"^[a-f0-9]{128}$")


class StreamedChatModel(FrozenContract):
    """A duplex destination's implemented input features, separate from v1."""

    id: Identifier
    name: str = Field(min_length=1, max_length=256)
    features: tuple[
        Literal[
            "image_input",
            "function_tools",
            "reasoning_toggle",
            "text_file_input",
            "pdf_input",
        ],
        ...,
    ] = Field(
        default=(),
        max_length=5,
        description=(
            "Implemented model features narrowed by the current owner policy; "
            "file features apply only to inline text/PDF input."
        ),
    )


class StreamedCatalog(FrozenContract):
    """Fresh delegated models reachable through the duplex body contract."""

    contract: Literal["chat.completions@0.2.0"] = STREAMED_CHAT_CONTRACT
    target_node: Identifier
    callable_id: Identifier
    callable_version: Literal["0.2.0"] = "0.2.0"
    descriptor_revision: str = Field(pattern=r"^[a-f0-9]{16}$")
    catalog_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: float = Field(gt=0)
    models: tuple[StreamedChatModel, ...] = Field(max_length=128)
    processing_domain: Literal["local", "private_site", "external"]
    usage: Literal["none", "included_only", "credits_possible", "unknown"]
    usage_label: str = Field(max_length=80)
    usage_url: str | None = Field(default=None, max_length=256)
    maximum_body_bytes: Literal[16777216] = MAX_BODY_BYTES


def encode_body(request: dict[str, JsonValue]) -> bytes:
    """Encode one bounded request for transport as raw media, not JSON escapes."""
    raw = json.dumps(
        request, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()
    InputBody.from_bytes(raw)
    return raw


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate request field")
        result[key] = value
    return result


async def receive_body_bytes(
    reference: InputBody, chunks: AsyncIterable[bytes]
) -> bytes:
    """Verify the entire half-closed body before parsing or upstream submission.

    The caller adapter must yield only correctly sequenced inline media chunks
    and return only after an empty successful input terminal. Cancellation and
    premature EOF must propagate as failures, never become a short body.
    """
    raw = bytearray()
    async for chunk in chunks:
        if (
            not chunk
            or len(chunk) > MAX_BODY_CHUNK_BYTES
            or len(raw) + len(chunk) > reference.size_bytes
        ):
            raise ValueError("request body exceeds signed bounds")
        raw.extend(chunk)
    if (
        len(raw) != reference.size_bytes
        or hashlib.sha256(raw).hexdigest() != reference.sha256
    ):
        raise ValueError("request body does not match signed reference")
    return bytes(raw)


async def receive_body(
    reference: InputBody, chunks: AsyncIterable[bytes]
) -> dict[str, JsonValue]:
    """Decode one completely verified media body, rejecting ambiguous JSON."""
    raw = await receive_body_bytes(reference, chunks)
    # Reject ambiguous JSON encodings before the strict request validator runs.
    try:
        value: object = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object
        )
    except RecursionError:
        raise ValueError("request JSON nesting exceeds parser bounds") from None
    return _OBJECT.validate_python(value)
