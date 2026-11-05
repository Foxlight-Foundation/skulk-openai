"""Private canonical Responses windows; opaque state is never a chat delta."""

import hashlib
import json
from typing import Literal, Self, final

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from skulk_inference_reference.continuation import Digest
from skulk_inference_reference.contracts import FrozenContract, Identifier
from skulk_inference_reference.input_body import MAX_BODY_BYTES

_ITEM = TypeAdapter(dict[str, JsonValue])
_OUTPUT_KINDS = frozenset({"message", "reasoning", "function_call", "compaction"})


@final
class CompactionProfile(FrozenContract):
    """Explicit trusted model facts and headroom, not an output-token limit.

    The owner must establish the context/output facts for the selected serving
    path. No profile is inferred from a model name or enabled by this class.
    A low threshold override can qualify compaction without filling the model.
    """

    model: Identifier
    context_tokens: int = Field(ge=1, le=16_000_000)
    reserved_output_tokens: int = Field(ge=1, le=16_000_000)
    threshold: int | None = Field(default=None, ge=1024)

    @model_validator(mode="after")
    def headroom(self) -> Self:
        """Leave explicit model-aware output headroom and reject impossible profiles."""
        if self.compact_threshold < 1024 or (
            self.compact_threshold > self.context_tokens - self.reserved_output_tokens
        ):
            raise ValueError("compaction profile leaves insufficient headroom")
        return self

    @property
    def compact_threshold(self) -> int:
        """Use an explicit override or the smaller safe relative model threshold."""
        return (
            self.threshold
            if self.threshold is not None
            else min(
                self.context_tokens * 3 // 4,
                self.context_tokens - self.reserved_output_tokens,
            )
        )

    @property
    def revision(self) -> str:
        """Bind protected windows to the exact trusted context-management profile."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


@final
class CanonicalWindow(FrozenContract):
    """Capability-private input items, serialized only into protected custody."""

    codec: Literal["openai.responses.window@0.1.0"] = "openai.responses.window@0.1.0"
    profile_revision: Digest | None = None
    items: tuple[dict[str, JsonValue], ...] = Field(
        min_length=1, max_length=4096, repr=False
    )

    def encode(self) -> bytes:
        """Encode exact provider fields with a fixed custody/transport byte bound."""
        raw = self.model_dump_json().encode()
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError("canonical window exceeds custody bound")
        return raw

    @classmethod
    def decode(cls, raw: bytes) -> "CanonicalWindow":
        """Decode only an authenticated local window, never a client-supplied item."""
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError("canonical window exceeds custody bound")
        window = cls.model_validate_json(raw)
        for item in window.items:
            kind = item.get("type", "message")
            if kind not in _OUTPUT_KINDS | {"function_call_output"}:
                raise ValueError("unsupported canonical item")
            if kind == "compaction":
                encrypted = item.get("encrypted_content")
                if not isinstance(encrypted, str) or not encrypted:
                    raise ValueError("missing opaque compaction content")
        return window


def _index(event: dict[str, JsonValue]) -> int:
    value = event.get("output_index")
    if type(value) is not int or not 0 <= value < 4096:
        raise ValueError("invalid output index")
    return value


def _item(value: JsonValue | None) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or value.get("type") not in _OUTPUT_KINDS:
        raise ValueError("unsupported canonical output")
    # Copy provider data so later event parsing cannot mutate a staged item.
    return _ITEM.validate_json(json.dumps(value, allow_nan=False))


class ResponseCapture:
    """Stage ordered canonical items, publishing a window only after clean EOF.

    Added/done pairs must agree. A compaction item is retained in the private
    window and never becomes visible text or a reasoning summary. Failure,
    interruption, malformed output or trailing events discard the candidate.
    """

    def __init__(
        self,
        input_items: tuple[dict[str, JsonValue], ...],
        *,
        profile_revision: str | None = None,
    ) -> None:
        """Bind one admitted provider input; no raw item is accepted from discovery."""
        self.input_items = input_items
        self.profile_revision = profile_revision
        self.added: dict[int, dict[str, JsonValue]] = {}
        self.done: dict[int, dict[str, JsonValue]] = {}
        self.completed = False
        self.window: bytes | None = None
        self.compactions = 0

    def observe(self, event: dict[str, JsonValue]) -> None:
        """Validate canonical item lifecycle while the regular parser owns deltas."""
        kind = event.get("type")
        if kind == "response.output_item.added":
            index, item = _index(event), _item(event.get("item"))
            item_kind = item.get("type")
            if item_kind == "compaction" and self.profile_revision is None:
                raise ValueError("compaction was not enabled")
            if index in self.added or not isinstance(item_kind, str):
                raise ValueError("duplicate canonical item")
            self.added[index] = item
        elif kind == "response.output_item.done":
            index, item = _index(event), _item(event.get("item"))
            added = self.added.get(index)
            if (
                index in self.done
                or added is None
                or added.get("type") != item.get("type")
            ):
                raise ValueError("unmatched canonical item")
            for field in ("id", "call_id", "name"):
                if added.get(field) is not None and added[field] != item.get(field):
                    raise ValueError("canonical identity changed")
            if item.get("type") == "compaction":
                encrypted = item.get("encrypted_content")
                if not isinstance(encrypted, str) or not encrypted:
                    raise ValueError("invalid compaction item")
            self.done[index] = item
        elif kind == "response.completed":
            response = event.get("response")
            if (
                not isinstance(response, dict)
                or response.get("status") != "completed"
                or not self.done
                or set(self.done) != set(self.added)
                or set(self.done) != set(range(len(self.done)))
            ):
                raise ValueError("incomplete canonical output")
            output = response.get("output")
            if not isinstance(output, list) or [_item(value) for value in output] != [
                self.done[index] for index in sorted(self.done)
            ]:
                raise ValueError("terminal/stream output mismatch")
            self.completed = True

    def validate_message(self, assistant: dict[str, JsonValue]) -> None:
        """Refuse canonical text/tools that disagree with public deltas."""
        text: list[str] = []
        calls: list[JsonValue] = []
        for index in sorted(self.done):
            item = self.done[index]
            if item.get("status") not in (None, "completed"):
                raise ValueError("canonical item did not complete")
            if item.get("type") == "message":
                content = item.get("content")
                if item.get("role") != "assistant" or not isinstance(content, list):
                    raise ValueError("invalid canonical message")
                for part in content:
                    if not isinstance(part, dict):
                        raise ValueError("invalid canonical content")
                    value = (
                        part.get("text")
                        if part.get("type") == "output_text"
                        else (
                            part.get("refusal")
                            if part.get("type") == "refusal"
                            else None
                        )
                    )
                    if not isinstance(value, str):
                        raise ValueError("unsupported canonical content")
                    text.append(value)
            elif item.get("type") == "function_call":
                identifier, name, arguments = (
                    item.get("call_id"),
                    item.get("name"),
                    item.get("arguments"),
                )
                if not all(
                    isinstance(value, str) for value in (identifier, name, arguments)
                ):
                    raise ValueError("invalid canonical function")
                calls.append(
                    {
                        "id": identifier,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    }
                )
        if assistant.get("content") != ("".join(text) or None):
            raise ValueError("canonical/public text mismatch")
        if assistant.get("tool_calls") != (calls or None):
            raise ValueError("canonical/public tools mismatch")

    def seal(self) -> None:
        """Publish stateless input, pruning only before its latest checkpoint."""
        if not self.completed:
            raise ValueError("canonical output is not complete")
        output = tuple(self.done[index] for index in sorted(self.done))
        items = (*self.input_items, *output)
        checkpoints = [
            i for i, item in enumerate(items) if item.get("type") == "compaction"
        ]
        self.compactions = sum(item.get("type") == "compaction" for item in output)
        if checkpoints:
            items = items[checkpoints[-1] :]
        self.window = CanonicalWindow(
            items=items, profile_revision=self.profile_revision
        ).encode()

    def discard(self) -> None:
        """Forget uncommitted items without exposing them in a diagnostic value."""
        self.window = None
        self.done.clear()
        self.added.clear()
        self.completed = False


class AssistantAssembler:
    """Assemble the exact public assistant message used in history fingerprints."""

    def __init__(
        self,
        *,
        allowed_functions: frozenset[str] | None = None,
        allow_parallel: bool = True,
    ) -> None:
        """Start content/summary/tool accumulators without executing any tool."""
        self.content: list[str] = []
        self.summary: list[str] = []
        self.tools: dict[int, dict[str, JsonValue]] = {}
        self.arguments: dict[int, list[str]] = {}
        self.allowed_functions = allowed_functions
        self.allow_parallel = allow_parallel

    def append(self, delta: dict[str, JsonValue]) -> None:
        """Accumulate visible text, supplied summaries and function deltas."""
        content, summary = delta.get("content"), delta.get("reasoning_content")
        if isinstance(content, str):
            self.content.append(content)
        if isinstance(summary, str):
            self.summary.append(summary)
        calls = delta.get("tool_calls")
        if not isinstance(calls, list):
            return
        for call in calls:
            if not isinstance(call, dict):
                raise ValueError("invalid public tool delta")
            index, function = call.get("index"), call.get("function")
            if type(index) is not int or not isinstance(function, dict):
                raise ValueError("invalid public tool delta")
            if index not in self.tools:
                identifier, name = call.get("id"), function.get("name")
                if not isinstance(identifier, str) or not isinstance(name, str):
                    raise ValueError("missing public tool identity")
                if (
                    not identifier
                    or len(identifier) > 256
                    or not name
                    or len(name) > 256
                    or (
                        self.allowed_functions is not None
                        and name not in self.allowed_functions
                    )
                    or (self.tools and not self.allow_parallel)
                ):
                    raise ValueError("public tool is not permitted")
                self.tools[index] = {
                    "id": identifier,
                    "type": "function",
                    "function": {"name": name, "arguments": ""},
                }
                self.arguments[index] = []
            target = self.tools[index]["function"]
            arguments = function.get("arguments")
            if not isinstance(target, dict) or not isinstance(arguments, str):
                raise ValueError("invalid public tool arguments")
            self.arguments[index].append(arguments)

    def message(self) -> dict[str, JsonValue]:
        """Return canonical public keys; tool-only content is explicitly null."""
        result: dict[str, JsonValue] = {
            "role": "assistant",
            "content": "".join(self.content) or None,
        }
        if self.summary:
            result["reasoning_content"] = "".join(self.summary)
        if self.tools:
            calls: list[JsonValue] = []
            for index in sorted(self.tools):
                tool = self.tools[index]
                function = tool["function"]
                if not isinstance(function, dict):
                    raise ValueError("invalid public tool state")
                calls.append(
                    {
                        **tool,
                        "function": {
                            **function,
                            "arguments": "".join(self.arguments[index]),
                        },
                    }
                )
            result["tool_calls"] = calls
        if not self.content and not self.tools:
            raise ValueError("empty public assistant output")
        return result
