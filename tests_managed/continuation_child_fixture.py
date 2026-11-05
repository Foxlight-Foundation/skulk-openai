"""Synthetic canonical upstream for real child/media/cancellation qualification."""

import asyncio
from collections.abc import AsyncIterator

import httpx
from pydantic import JsonValue, TypeAdapter

from skulk_openai import managed
from skulk_openai.responses import event_bytes

LARGE_TEXT = '🦊"\\\n' * 9000
LARGE_ARGUMENTS = '{"text":"' + "x" * 140000 + '"}'
_OBJECT = TypeAdapter(dict[str, JsonValue])


class FixtureResponse(httpx.AsyncByteStream):
    """Hold a call indefinitely or emit large canonical text/function items."""

    def __init__(self, *, hold: bool) -> None:
        """Select behavior from a synthetic-only fixture entrypoint."""
        self.hold = hold

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Use complete canonical lifecycle pairs, pausing only in the hold case."""
        text = "synthetic text" if self.hold else LARGE_TEXT
        item: dict[str, JsonValue] = {
            "type": "message",
            "id": "fixture-message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        output: list[JsonValue] = [item]
        yield event_bytes(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "status": "in_progress", "content": []},
            }
        )
        yield event_bytes({"type": "response.output_text.delta", "delta": text})
        if self.hold:
            await asyncio.Event().wait()
        yield event_bytes(
            {"type": "response.output_item.done", "output_index": 0, "item": item}
        )
        if not self.hold:
            call: dict[str, JsonValue] = {
                "type": "function_call",
                "id": "fixture-function",
                "call_id": "fixture-call",
                "name": "echo",
                "arguments": LARGE_ARGUMENTS,
                "status": "completed",
            }
            output.append(call)
            yield event_bytes(
                {
                    "type": "response.output_item.added",
                    "output_index": 1,
                    "item": {**call, "status": "in_progress", "arguments": ""},
                }
            )
            yield event_bytes(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fixture-function",
                    "delta": LARGE_ARGUMENTS,
                }
            )
            yield event_bytes(
                {"type": "response.output_item.done", "output_index": 1, "item": call}
            )
        yield event_bytes(
            {
                "type": "response.completed",
                "response": {"status": "completed", "output": output},
            }
        )

    async def aclose(self) -> None:
        """Fixture has no socket, file or retained media resource."""


def fixture_transport() -> httpx.MockTransport:
    """Reject every non-Responses path; no issuer or real network is reachable."""

    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) != "https://api.openai.com/v1/responses":
            return httpx.Response(503)
        body = _OBJECT.validate_json(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=FixtureResponse(
                hold="HOLD_SOURCE_FIXTURE" in str(body.get("input"))
            ),
        )

    return httpx.MockTransport(handle)


if __name__ == "__main__":
    managed.mock_transport = fixture_transport
    managed.main()
