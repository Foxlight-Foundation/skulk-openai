"""Fragmentation, completion, bounds and owned stream cleanup qualification."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing

import httpx
import pytest
from pydantic import JsonValue, ValidationError

from skulk_inference_reference.contracts import (
    GenerationEvent,
    GenerationRequest,
    Message,
)
from skulk_openai.responses import MAX_EVENT_BYTES, MockResponsesAdapter, event_bytes


class Fragments(httpx.AsyncByteStream):
    """A cancellable synthetic byte source with observable finalization."""

    def __init__(self, chunks: tuple[bytes, ...], *, stall: bool = False) -> None:
        """Retain chunks and an optional never-ending wait after their delivery."""
        self.chunks = chunks
        self.stall = stall
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Deliver bytes without coalescing small text deltas behind a buffer."""
        for chunk in self.chunks:
            yield chunk
        if self.stall:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        """Record the caller's cleanup of the simulated HTTP stream."""
        self.closed = True


def adapter_for(
    stream: Fragments, *, status: int = 200, duration_seconds: float = 30
) -> MockResponsesAdapter:
    """Create a socket-free transport that returns the supplied synthetic stream."""
    return MockResponsesAdapter(
        httpx.MockTransport(
            lambda _: httpx.Response(
                status,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )
        ),
        duration_seconds=duration_seconds,
    )


def request() -> GenerationRequest:
    """Return a small text request with no authentication state."""
    return GenerationRequest(messages=(Message(role="user", content="hello"),))


async def test_fragmented_utf8_and_crlf_complete_only_at_terminal() -> None:
    raw = (
        event_bytes({"type": "response.output_text.delta", "delta": "hello 🦊"})
        + event_bytes({"type": "response.completed"})
    ).replace(b"\n", b"\r\n")
    stream = Fragments(tuple(bytes((byte,)) for byte in raw))
    events = [
        item
        async for item in adapter_for(stream).generate(request(), model="synthetic")
    ]
    assert [(item.kind, item.text) for item in events] == [
        ("text", "hello 🦊"),
        ("completed", None),
    ]
    assert stream.closed


@pytest.mark.parametrize(
    "terminal,error",
    [
        (None, "interrupted"),
        ("response.incomplete", "incomplete"),
        ("response.failed", "upstream_failed"),
        ("error", "upstream_failed"),
    ],
)
async def test_partial_text_does_not_imply_success(
    terminal: str | None, error: str
) -> None:
    chunks = [event_bytes({"type": "response.output_text.delta", "delta": "partial"})]
    if terminal is not None:
        chunks.append(
            event_bytes({"type": terminal, "message": "sensitive upstream detail"})
        )
    stream = Fragments(tuple(chunks))
    events = [
        item
        async for item in adapter_for(stream).generate(request(), model="synthetic")
    ]
    assert events[0].kind == "text" and events[-1].error == error
    assert not any(item.kind == "completed" for item in events)
    assert "sensitive" not in repr(events)
    assert stream.closed


@pytest.mark.parametrize(
    "body",
    [
        b'data: {"type":"response.completed","type":"response.failed"}\n\n',
        b'data: {"type":"response.completed","value":NaN}\n\n',
        b'data: {"type":"response.completed"}',
        b"data: []\n\n",
        b"data: \xff\n\n",
        b"data: " + b"x" * (MAX_EVENT_BYTES + 1) + b"\n\n",
        event_bytes({"type": "response.function_call_arguments.delta", "delta": "{}"}),
    ],
)
async def test_invalid_or_unsupported_events_fail_closed(body: bytes) -> None:
    stream = Fragments((body,))
    events = [
        item
        async for item in adapter_for(stream).generate(request(), model="synthetic")
    ]
    assert len(events) == 1 and events[0].error == "invalid_stream"
    assert stream.closed


async def test_output_limit_closes_without_replay() -> None:
    stream = Fragments(
        (event_bytes({"type": "response.output_text.delta", "delta": "too long"}),)
    )
    item = GenerationRequest(messages=request().messages, maximum_output_characters=1)
    events = [
        event async for event in adapter_for(stream).generate(item, model="synthetic")
    ]
    assert len(events) == 1 and events[0].error == "output_limit"
    assert stream.closed


async def test_small_delta_is_delivered_before_upstream_stall() -> None:
    stream = Fragments(
        (event_bytes({"type": "response.output_text.delta", "delta": "a"}),), stall=True
    )
    async with aclosing(
        adapter_for(stream).generate(request(), model="synthetic")
    ) as output:
        async with asyncio.timeout(1):
            first = await anext(output)
        assert first.text == "a"
    assert stream.closed


async def test_cancellation_closes_http_without_success_or_replay() -> None:
    stream = Fragments((), stall=True)
    output = adapter_for(stream).generate(request(), model="synthetic")
    execution = asyncio.create_task(anext(output))
    await asyncio.wait_for(stream.waiting.wait(), 1)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    await output.aclose()
    assert stream.closed


async def test_http_failure_does_not_expose_body_or_retry() -> None:
    stream = Fragments((b"private account error detail",))
    events = [
        item
        async for item in adapter_for(stream, status=429).generate(
            request(), model="synthetic"
        )
    ]
    assert len(events) == 1 and events[0].error == "upstream_failed"
    assert "private" not in repr(events) and stream.closed


async def test_upstream_request_uses_supported_fields_only() -> None:
    payloads: list[bytes] = []

    def respond(incoming: httpx.Request) -> httpx.Response:
        payloads.append(incoming.content)
        assert "authorization" not in incoming.headers
        assert incoming.url.path == "/v1/responses"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=event_bytes({"type": "response.completed"}),
        )

    events = [
        item
        async for item in MockResponsesAdapter(httpx.MockTransport(respond)).generate(
            request(), model="synthetic"
        )
    ]
    assert len(payloads) == 1 and events[-1].kind == "completed"
    assert b'"store":false' in payloads[0] and b'"stream":true' in payloads[0]
    assert b"max_output_tokens" not in payloads[0]


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "text"},
        {"kind": "failed"},
        {"kind": "failed", "error": "incomplete", "text": "do not retain"},
        {"kind": "completed", "error": "upstream_failed"},
    ],
)
def test_common_event_shape_rejects_ambiguous_terminal(
    payload: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValidationError):
        GenerationEvent.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "text"},
        {"kind": "failed"},
        {"kind": "failed", "error": "incomplete", "text": "do not retain"},
        {"kind": "completed", "error": "upstream_failed"},
    ],
)
def test_exported_schema_matches_terminal_shape(payload: dict[str, JsonValue]) -> None:
    import jsonschema

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, GenerationEvent.model_json_schema())


async def test_real_network_transport_is_refused() -> None:
    transport = httpx.AsyncHTTPTransport()
    try:
        with pytest.raises(TypeError, match="mock transport"):
            MockResponsesAdapter(transport)
    finally:
        await transport.aclose()


@pytest.mark.parametrize(
    "trailing",
    [
        {"type": "response.completed"},
        {"type": "response.failed"},
        {"type": "response.output_text.delta", "delta": "after completion"},
        {"type": "unsupported.event"},
    ],
)
async def test_terminal_is_withheld_until_trailing_events_are_validated(
    trailing: dict[str, JsonValue],
) -> None:
    stream = Fragments(
        (event_bytes({"type": "response.completed"}) + event_bytes(trailing),)
    )
    events = [
        item
        async for item in adapter_for(stream).generate(request(), model="synthetic")
    ]
    assert len(events) == 1 and events[0].error == "invalid_stream"
    assert stream.closed


async def test_terminal_without_eof_has_a_bounded_failure() -> None:
    stream = Fragments((event_bytes({"type": "response.completed"}),), stall=True)
    async with asyncio.timeout(2):
        events = [
            item
            async for item in adapter_for(stream).generate(request(), model="synthetic")
        ]
    assert len(events) == 1 and events[0].error == "invalid_stream"
    assert stream.closed


async def test_upstream_deadline_does_not_cancel_slow_consumer_work() -> None:
    stream = Fragments(
        (
            event_bytes({"type": "response.output_text.delta", "delta": "text"}),
            event_bytes({"type": "response.completed"}),
        )
    )
    output = adapter_for(stream, duration_seconds=0.02).generate(
        request(), model="synthetic"
    )
    async with aclosing(output):
        assert (await anext(output)).kind == "text"
        await asyncio.sleep(0.04)
        assert (await anext(output)).error == "invalid_stream"
    assert stream.closed
