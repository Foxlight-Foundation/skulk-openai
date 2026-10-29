"""Bounded Responses event adaptation against an injected, credential-free client."""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Literal

import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError

from skulk_inference_reference.contracts import GenerationEvent, GenerationRequest

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
MAX_EVENT_BYTES = 16_384
MAX_STREAM_BYTES = 262_144
MAX_EVENTS = 256


class InvalidResponseStream(Exception):
    """An upstream fixture exceeded the bounded text-only reference contract."""


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidResponseStream("duplicate event key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise InvalidResponseStream("nonfinite event value")


async def server_events(
    response: httpx.Response,
) -> AsyncGenerator[dict[str, JsonValue]]:
    """Parse fragmented SSE with byte/event bounds before JSON decoding.

    Metadata events are parsed and counted too. Arbitrary upstream bodies never
    become error messages, and EOF without an explicit terminal is not success.
    """
    buffer = b""
    data: list[bytes] = []
    event_bytes = 0
    total = 0
    count = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_STREAM_BYTES:
            raise InvalidResponseStream("stream exceeds byte bound")
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.removesuffix(b"\r")
            event_bytes += len(line) + 1
            if event_bytes > MAX_EVENT_BYTES:
                raise InvalidResponseStream("event exceeds byte bound")
            if not line:
                if data:
                    count += 1
                    if count > MAX_EVENTS:
                        raise InvalidResponseStream("too many events")
                    try:
                        raw: object = json.loads(
                            b"\n".join(data),
                            object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant,
                        )
                        event = _JSON_OBJECT.validate_python(raw, strict=True)
                    except (ValueError, ValidationError, RecursionError) as error:
                        raise InvalidResponseStream("invalid event JSON") from error
                    yield event
                data = []
                event_bytes = 0
            elif line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
        if event_bytes + len(buffer) > MAX_EVENT_BYTES:
            raise InvalidResponseStream("event exceeds byte bound")
    if buffer or data:
        raise InvalidResponseStream("unterminated event")


class MockResponsesAdapter:
    """Translate synthetic OpenAI events through a caller-supplied MockTransport.

    This deliberately refuses a real HTTP client transport. Live credentials,
    authentication, retries and network requests are outside this first slice.
    """

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        """Retain an explicit mock; never read account credentials or environment."""
        if not isinstance(transport, httpx.MockTransport):
            raise TypeError("the reference accepts only an explicit mock transport")
        self._transport = transport

    async def generate(
        self,
        request: GenerationRequest,
        *,
        model: str,
    ) -> AsyncGenerator[GenerationEvent]:
        """Yield bounded text and exactly one observed completion/failure.

        Caller cancellation propagates without being converted into success or
        automatically replaying a request. Closing the iterator closes HTTP.
        """
        terminal: GenerationEvent | None = None
        characters = 0
        try:
            async with asyncio.timeout(30.0):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    trust_env=False,
                    timeout=5.0,
                ) as client:
                    async with client.stream(
                        "POST",
                        "https://mock-openai.invalid/v1/responses",
                        json={
                            "model": model,
                            "store": False,
                            "stream": True,
                            "input": [
                                message.model_dump() for message in request.messages
                            ],
                        },
                    ) as response:
                        if response.status_code != 200:
                            yield GenerationEvent(
                                kind="failed", error="upstream_failed"
                            )
                            return
                        media_type = response.headers.get("content-type", "").split(
                            ";"
                        )[0]
                        if media_type != "text/event-stream":
                            raise InvalidResponseStream("expected event stream")
                        async with aclosing(server_events(response)) as events:
                            while True:
                                try:
                                    if terminal is None:
                                        event = await anext(events)
                                    else:
                                        # Hold success for bounded EOF; reject any
                                        # contradictory trailing event.
                                        event = await asyncio.wait_for(
                                            anext(events), 0.5
                                        )
                                except StopAsyncIteration:
                                    break
                                if terminal is not None:
                                    raise InvalidResponseStream(
                                        "event follows terminal"
                                    )
                                event_type = event.get("type")
                                if event_type == "response.output_text.delta":
                                    delta = event.get("delta")
                                    if not isinstance(delta, str) or not delta:
                                        raise InvalidResponseStream(
                                            "invalid text delta"
                                        )
                                    characters += len(delta)
                                    if characters > request.maximum_output_characters:
                                        yield GenerationEvent(
                                            kind="failed", error="output_limit"
                                        )
                                        return
                                    yield GenerationEvent(kind="text", text=delta)
                                elif event_type == "response.completed":
                                    terminal = GenerationEvent(kind="completed")
                                elif event_type in (
                                    "response.failed",
                                    "response.incomplete",
                                    "error",
                                ):
                                    code: Literal["incomplete", "upstream_failed"] = (
                                        "incomplete"
                                        if event_type == "response.incomplete"
                                        else "upstream_failed"
                                    )
                                    terminal = GenerationEvent(
                                        kind="failed", error=code
                                    )
                                elif event_type not in (
                                    "response.created",
                                    "response.in_progress",
                                    "response.output_item.added",
                                    "response.content_part.added",
                                    "response.output_text.done",
                                    "response.content_part.done",
                                    "response.output_item.done",
                                ):
                                    raise InvalidResponseStream(
                                        "unsupported stream event"
                                    )
            yield terminal or GenerationEvent(kind="failed", error="interrupted")
        except (InvalidResponseStream, httpx.HTTPError, UnicodeError, TimeoutError):
            yield GenerationEvent(kind="failed", error="invalid_stream")


def event_bytes(event: dict[str, JsonValue]) -> bytes:
    """Encode a synthetic SSE event for tests and the offline demo."""
    return b"data: " + json.dumps(event, allow_nan=False).encode() + b"\n\n"


def text_transport(
    *, text: str = "Hello from the mock OpenAI capability."
) -> httpx.MockTransport:
    """Build a credential-free synthetic Responses service; it opens no sockets."""

    async def respond(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=event_bytes({"type": "response.output_text.delta", "delta": text})
            + event_bytes({"type": "response.completed"}),
        )

    return httpx.MockTransport(respond)
