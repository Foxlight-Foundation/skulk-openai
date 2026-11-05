"""Provider-neutral author-defined continuation events over public SDK frames."""

from collections.abc import Iterator
from typing import Self

from pydantic import JsonValue, model_validator

from .chat import ChatEvent
from .continuation import ContinuationReceipt, FailureCode
from .contracts import FrozenContract

MAX_DELTA_CHARACTERS = 2048


class ContinuationEvent(FrozenContract):
    """Public chat event plus a committed receipt or bounded recovery category."""

    event: ChatEvent
    receipt: ContinuationReceipt | None = None
    continuation_error: FailureCode | None = None

    @model_validator(mode="after")
    def terminal_shape(self) -> Self:
        """Only committed successful terminals may carry a receipt."""
        if (self.event.kind == "completed") != (self.receipt is not None):
            raise ValueError("completion requires committed continuation receipt")
        if self.continuation_error is not None and self.event.kind != "failed":
            raise ValueError("continuation error requires a failed event")
        return self


class ReceiptOutcome(FrozenContract):
    """A current authenticated receipt or fixed failure, never stored content."""

    receipt: ContinuationReceipt | None = None
    error: FailureCode | None = None

    @model_validator(mode="after")
    def exclusive(self) -> Self:
        """Require exactly one successful result or refusal classification."""
        if (self.receipt is None) == (self.error is None):
            raise ValueError("receipt outcome requires one result")
        return self


def continuation_chunks(event: ContinuationEvent) -> Iterator[ContinuationEvent]:
    """Split public deltas without changing text, tool identity or arguments.

    At most 2,048 Unicode characters per fragment leave room for worst-case
    JSON escaping and bounded tool metadata beneath the SDK's 64 KiB header.
    No private provider state is represented in this wire contract.
    """
    if event.event.kind != "delta":
        yield event
        return
    delta = event.event.delta
    if set(delta) - {"content", "reasoning_content", "tool_calls"}:
        raise ValueError("unsupported public delta")
    for field in ("content", "reasoning_content"):
        if field not in delta:
            continue
        value = delta[field]
        if not isinstance(value, str) or not value:
            raise ValueError("invalid public text delta")
        for offset in range(0, len(value), MAX_DELTA_CHARACTERS):
            yield ContinuationEvent(
                event=ChatEvent(
                    kind="delta",
                    delta={field: value[offset : offset + MAX_DELTA_CHARACTERS]},
                )
            )
    if "tool_calls" not in delta:
        return
    calls = delta["tool_calls"]
    if not isinstance(calls, list) or not 1 <= len(calls) <= 32:
        raise ValueError("invalid public tools")
    for call in calls:
        if not isinstance(call, dict) or set(call) - {
            "index",
            "id",
            "type",
            "function",
        }:
            raise ValueError("invalid public tool")
        index, function = call.get("index"), call.get("function")
        if (
            type(index) is not int
            or not 0 <= index < 32
            or not isinstance(function, dict)
        ):
            raise ValueError("invalid public tool")
        if set(function) - {"name", "arguments"}:
            raise ValueError("unsupported public function")
        for value in (call.get("id"), function.get("name")):
            if value is not None and (
                not isinstance(value, str) or not 1 <= len(value) <= 256
            ):
                raise ValueError("invalid public tool identity")
        if call.get("type") not in (None, "function"):
            raise ValueError("unsupported public tool type")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            raise ValueError("invalid public tool arguments")
        for offset in range(0, max(1, len(arguments)), MAX_DELTA_CHARACTERS):
            fragment: dict[str, JsonValue] = (
                {
                    **call,
                    "function": {
                        **function,
                        "arguments": arguments[:MAX_DELTA_CHARACTERS],
                    },
                }
                if offset == 0
                else {
                    "index": index,
                    "function": {
                        "arguments": arguments[offset : offset + MAX_DELTA_CHARACTERS]
                    },
                }
            )
            yield ContinuationEvent(
                event=ChatEvent(kind="delta", delta={"tool_calls": [fragment]})
            )
