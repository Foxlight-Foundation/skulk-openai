"""Credential-free public probe boundaries, including late failure and no replay."""

import json
from collections.abc import AsyncIterator
from typing import NoReturn

import httpx
import pytest

from examples.qualify_models import (
    EXPECTED,
    MEDIA_FEATURES,
    Object,
    ProbeFailure,
    Qualification,
    Report,
    StreamCheck,
    main,
    plan,
)

MODEL = "capability/fixture.chat/text"
DIGEST = "b" * 64


def catalog(*, media: bool = True) -> bytes:
    """Return unrelated managed metadata; no OpenAI identity or token is needed."""
    return json.dumps(
        {
            "data": [
                {
                    "id": MODEL,
                    "inference_capability": {
                        "contract": "chat.completions@0.2.0",
                        "features": sorted(MEDIA_FEATURES) if media else [],
                    },
                }
            ]
        }
    ).encode()


def inventory(digest: str = DIGEST) -> bytes:
    """Bind one independently identified fixture descriptor to its live runtime."""
    return json.dumps(
        {
            "installations": [
                {
                    "release": {
                        "bundle_id": "fixture",
                        "bundle_version": "1.0.0",
                        "registration": {
                            "capabilities": [
                                {
                                    "qualified_id": "fixture.chat@0.2.0",
                                    "revision": "a" * 16,
                                }
                            ]
                        },
                    },
                    "selected_digest": digest,
                    "enabled": True,
                    "stale": False,
                    "uninstalled": False,
                    "service": {"state": "running", "active_digest": digest},
                }
            ]
        }
    ).encode()


def prepared(*, media: bool = False, advertised: bool = True) -> Report:
    """Plan a fixture with no network effects."""
    return plan(
        catalog(media=advertised),
        inventory(),
        (MODEL,),
        all_enabled=False,
        media=media,
        now=1000.0,
    )


def event(value: Object | str) -> bytes:
    """Encode one public SSE record."""
    text = value if isinstance(value, str) else json.dumps(value)
    return ("data: " + text + "\n\n").encode()


STREAM = (
    event(
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello"}}]}
    )
    + event({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    + event("[DONE]")
)


@pytest.mark.parametrize("size", [1, 2, 7, 8192])
def test_stream_requires_complete_records_across_byte_boundaries(size: int) -> None:
    """Packet boundaries cannot change terminal or text-count evidence."""
    parser = StreamCheck()
    for offset in range(0, len(STREAM), size):
        parser.feed(STREAM[offset : offset + size])
    assert parser.complete() == 5


@pytest.mark.parametrize(
    "bad",
    [
        STREAM[:-1],
        STREAM.split(b"data: [DONE]")[0],
        event("[DONE]"),
        STREAM + event({"error": {"message": "synthetic secret"}}),
        STREAM + event({"choices": [{"delta": {"content": "late"}}]}),
        event(
            {"choices": [{"delta": {"content": "partial"}, "finish_reason": "length"}]}
        ),
        event({"choices": [{"delta": {"role": "user", "content": "wrong role"}}]}),
    ],
)
def test_invalid_stream_never_becomes_success(bad: bytes) -> None:
    """Partial output, wrong roles and late events must fail closed."""
    with pytest.raises(ProbeFailure):
        parser = StreamCheck()
        parser.feed(bad)
        _ = parser.complete()


def test_unterminated_stream_bytes_are_bounded_before_decode() -> None:
    """A missing newline cannot grow an unbounded parser buffer."""
    with pytest.raises(ProbeFailure, match="response_bound"):
        StreamCheck().feed(b"x" * 32769)


def test_planning_preserves_untested_feature_distinctions() -> None:
    """Missing media claims skip that case without hiding a text model."""
    report = prepared(media=True, advertised=False)
    assert not report.executed and not report.passed and not report.complete
    assert [case.outcome for case in report.cases] == ["not_run", "not_advertised"]
    assert all(case.submitted_requests == 0 for case in report.cases)


def test_unknown_or_duplicate_model_selection_is_refused_before_egress() -> None:
    """Only unique currently advertised concrete models enter a plan."""
    for models in [("missing",), (MODEL, MODEL)]:
        with pytest.raises(ValueError):
            _ = plan(
                catalog(),
                inventory(),
                models,
                all_enabled=False,
                media=True,
                now=1000.0,
            )


class BrokenTransport(httpx.AsyncByteStream):
    """Yield semantic success followed by a transport failure."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """The parser must not publish success before observing this error."""
        yield STREAM
        raise httpx.ReadError("synthetic secret must not enter receipt")


async def test_late_transport_failure_is_recorded_once_without_replay() -> None:
    """A semantic finish cannot conceal subsequent transport failure."""
    calls = 0
    reports: list[Report] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "GET":
            return httpx.Response(200, content=inventory())
        calls += 1
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=BrokenTransport()
        )

    async with httpx.AsyncClient(
        base_url="http://fixture.invalid", transport=httpx.MockTransport(handle)
    ) as client:
        result = await Qualification(client, reports.append).execute(prepared())
    assert calls == 1 and not result.passed
    assert result.cases[0].failure == "transport_error"
    assert result.cases[0].submitted_requests == 1
    assert any(
        report.cases[0].outcome == "submitted" and not report.complete
        for report in reports
    )
    assert "synthetic secret" not in result.model_dump_json()


async def test_deadline_cancels_inflight_request_without_retry() -> None:
    """The whole-case deadline closes work and records one attempt."""
    import asyncio

    closed = False
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal closed, calls
        if request.method == "GET":
            return httpx.Response(200, content=inventory())
        calls += 1
        try:
            await asyncio.Event().wait()
        finally:
            closed = True
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(
        base_url="http://fixture.invalid", transport=httpx.MockTransport(handle)
    ) as client:
        result = await Qualification(
            client, lambda _: None, deadline_seconds=0.01
        ).execute(prepared())
    assert closed and calls == 1
    assert result.cases[0].failure == "deadline" and not result.passed


async def test_media_tool_history_and_schema_observations_remain_separate() -> None:
    """Preserve public tool IDs while keeping valid answer quality separate."""
    from pydantic import TypeAdapter

    decoder = TypeAdapter(Object)
    calls: list[Object] = []
    values = {**EXPECTED, "color": "synthetic alternate observation"}

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, content=inventory())
        body = decoder.validate_json(request.content)
        calls.append(body)
        if body["stream"] is True:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=STREAM
            )
        if len(calls) == 2:
            messages = body["messages"]
            assert isinstance(messages, list) and isinstance(messages[0], dict)
            parts = messages[0]["content"]
            assert isinstance(parts, list) and len(parts) == 4
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "fixture-call",
                                        "type": "function",
                                        "function": {
                                            "name": "return_fixture",
                                            "arguments": json.dumps(values),
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            )
        history = body["messages"]
        assert isinstance(history, list) and isinstance(history[2], dict)
        assert history[2]["tool_call_id"] == "fixture-call"
        assert history[2]["content"] == json.dumps(values)
        assert body["tool_choice"] == "none" and "response_format" in body
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(values)},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="http://fixture.invalid", transport=httpx.MockTransport(handle)
    ) as client:
        result = await Qualification(client, lambda _: None).execute(
            prepared(media=True)
        )
    assert len(calls) == 3 and result.passed
    assert result.cases[1].submitted_requests == 2
    assert result.cases[1].synthetic_facts_match is False
    assert result.cases[1].tool_result_preserved is True
    assert "synthetic alternate observation" not in result.model_dump_json()
    assert "fixture-call" not in result.model_dump_json()


async def test_changed_runtime_invalidates_aggregate_without_replaying() -> None:
    """A replaced runtime cannot inherit the earlier qualification."""
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "GET":
            return httpx.Response(200, content=inventory("c" * 64))
        calls += 1
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=STREAM
        )

    async with httpx.AsyncClient(
        base_url="http://fixture.invalid", transport=httpx.MockTransport(handle)
    ) as client:
        result = await Qualification(client, lambda _: None).execute(prepared())
        assert not result.passed and result.runtime_stable is False
        with pytest.raises(ValueError, match="fresh plan"):
            _ = await Qualification(client, lambda _: None).execute(result)
    assert calls == 1 and result.cases[0].outcome == "passed"


def test_execution_needs_explicit_models_and_a_new_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execution preflight must finish before an HTTP client is created."""

    def forbid(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("Preflight must not create an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", forbid)
    monkeypatch.setattr("sys.argv", ["qualify_models", "--execute"])
    assert main() == 1


async def test_receipt_sink_failure_prevents_inference() -> None:
    """No request starts when the local evidence sink cannot accept a receipt."""
    calls = 0

    async def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("Receipt custody must precede network effects")

    def refuse(_report: Report) -> None:
        raise OSError("Synthetic receipt failure")

    async with httpx.AsyncClient(
        base_url="http://fixture.invalid", transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(OSError):
            _ = await Qualification(client, refuse).execute(prepared())
    assert calls == 0


async def test_deadline_must_be_finite_and_positive() -> None:
    """Injected clocks cannot accidentally disable the whole-request deadline."""
    async with httpx.AsyncClient() as client:
        for deadline in [float("inf"), float("nan"), -1.0, 0.0]:
            with pytest.raises(ValueError):
                _ = Qualification(client, lambda _: None, deadline_seconds=deadline)


async def test_large_additive_model_catalog_remains_metadata_only() -> None:
    """A normal large catalog must not trip the smaller installation read bound."""
    from pydantic import TypeAdapter

    value = TypeAdapter(Object).validate_json(catalog())
    value["additive_metadata"] = "x" * 1100000
    body = json.dumps(value).encode()
    calls: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.method == "GET"
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(
        base_url="http://fixture.invalid", transport=httpx.MockTransport(handle)
    ) as client:
        read = await Qualification(client, lambda _: None).read("/v1/models")
    report = plan(
        read, inventory(), (MODEL,), all_enabled=False, media=False, now=1000.0
    )
    assert calls == ["/v1/models"] and not report.executed
    assert report.cases[0].outcome == "not_run"
