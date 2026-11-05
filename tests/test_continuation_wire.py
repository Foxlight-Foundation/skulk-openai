"""Public assembly stays identical across bounded transport fragmentation."""

import json

import pytest
from pydantic import JsonValue

from skulk_inference_reference.chat import ChatEvent
from skulk_inference_reference.continuation_wire import (
    ContinuationEvent,
    continuation_chunks,
)


def test_large_unicode_text_summary_and_tool_arguments_fit_public_frames() -> None:
    text = '🦊"\\\n' * 9000
    arguments = json.dumps({"text": text}, ensure_ascii=False)
    delta: dict[str, JsonValue] = {
        "content": text,
        "reasoning_content": text,
        "tool_calls": [
            {
                "index": 0,
                "id": "call-1",
                "type": "function",
                "function": {"name": "echo", "arguments": arguments},
            }
        ],
    }
    fragments = list(
        continuation_chunks(
            ContinuationEvent(event=ChatEvent(kind="delta", delta=delta))
        )
    )
    assert len(fragments) > 30
    assert max(len(item.model_dump_json().encode()) for item in fragments) < 49152
    for key in ("content", "reasoning_content"):
        assert "".join(str(item.event.delta.get(key, "")) for item in fragments) == text
    calls: list[dict[str, JsonValue]] = []
    for fragment in fragments:
        values = fragment.event.delta.get("tool_calls")
        if values is not None:
            assert isinstance(values, list)
            for value in values:
                assert isinstance(value, dict)
                calls.append(value)
    assert calls[0]["id"] == "call-1"
    assert sum("id" in call for call in calls) == 1
    reconstructed: list[str] = []
    for call in calls:
        function = call["function"]
        assert isinstance(function, dict) and call["index"] == 0
        reconstructed.append(str(function["arguments"]))
    assert "".join(reconstructed) == arguments


@pytest.mark.parametrize("field", ["content", "reasoning_content"])
def test_unknown_or_unbounded_delta_metadata_is_refused(field: str) -> None:
    with pytest.raises(ValueError):
        list(
            continuation_chunks(
                ContinuationEvent(
                    event=ChatEvent(
                        kind="delta", delta={field: "hello", "unknown": "x" * 100000}
                    )
                )
            )
        )
