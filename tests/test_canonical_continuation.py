"""Guarded HTTP-mock roundtrips over actual caller identity and encrypted custody."""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue, TypeAdapter, ValidationError

from skulk_inference_reference.continuation import ContinuationReceipt
from skulk_inference_reference.continuation_auth import (
    ContinuationCredential,
    ContinuationProof,
)
from skulk_inference_reference.continuation_wire import ContinuationEvent
from skulk_inference_reference.input_body import InputBody, encode_body
from skulk_openai.canonical import CanonicalWindow, CompactionProfile
from skulk_openai.checkpoints import CheckpointLease, CheckpointStore
from skulk_openai.continuation_service import ContinuationService
from skulk_openai.provider import ProviderClient
from skulk_openai.responses import event_bytes
from tests.test_checkpoints import NEXT, USER, query, signed
from tests.test_gateway_chat import configured

_OBJECT = TypeAdapter(dict[str, JsonValue])
_PROFILE = CompactionProfile(
    model="text-a", context_tokens=65536, reserved_output_tokens=8192, threshold=1024
)


@pytest.mark.parametrize("change", ["exact", "tamper", "forged", "incomplete"])
async def test_signed_media_keeps_exact_bytes_and_authenticates_before_read(
    tmp_path: Path, change: str
) -> None:
    api = ResponseAPI(lambda _: output_events([text_output()]))
    store, credential, provider = await integrated(tmp_path, api)
    proof, raw = signed(store, credential, [USER])
    # Noncanonical whitespace must survive transport; re-encoding after parsing
    # would invalidate this exact-body signature at journal admission.
    raw = json.dumps(_OBJECT.validate_json(raw), indent=3).encode()
    proof = credential.sign(
        proof.request.model_copy(update={"body": InputBody.from_bytes(raw)})
    )
    if change == "forged":
        proof = proof.model_copy(update={"signature": "0" * 128})
    consumed = False

    async def chunks() -> AsyncIterator[bytes]:
        nonlocal consumed
        consumed = True
        yield raw if change != "tamper" else raw.replace(b"user", b"tool", 1)
        if change == "incomplete":
            raise ValueError("input did not half-close")

    with store.worker() as worker:
        events = [
            event
            async for event in ContinuationService(worker, provider).generate_streamed(
                proof, chunks()
            )
        ]
        assert not worker.leases
    assert consumed == (change != "forged")
    assert events[-1].event.kind == ("completed" if change == "exact" else "failed")
    assert len(api.requests) == (1 if change == "exact" else 0)


def output_events(output: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    """Use standard item lifecycle events and exact completed canonical output."""
    events: list[dict[str, JsonValue]] = []
    for index, item in enumerate(output):
        added = {**item}
        kind = item["type"]
        if kind == "function_call":
            added["arguments"] = ""
        if kind == "message":
            added["content"] = []
        events.append(
            {"type": "response.output_item.added", "output_index": index, "item": added}
        )
        if kind == "message":
            events.append({"type": "response.output_text.delta", "delta": "hello"})
        elif kind == "function_call":
            events.append(
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item["id"],
                    "delta": item["arguments"],
                }
            )
        events.append(
            {"type": "response.output_item.done", "output_index": index, "item": item}
        )
    events.append(
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": list(output),
                "usage": {
                    "input_tokens": 10000,
                    "output_tokens": 10,
                    "total_tokens": 10010,
                },
            },
        }
    )
    return events


def text_output() -> dict[str, JsonValue]:
    """One complete public assistant message."""
    return {
        "type": "message",
        "id": "msg-1",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "hello", "annotations": []}],
    }


def compacted_output() -> list[dict[str, JsonValue]]:
    """Match the previously observed before/after-message compaction item order."""
    return [
        {"type": "compaction", "id": "cmp-1", "encrypted_content": "opaque-before"},
        text_output(),
        {"type": "compaction", "id": "cmp-2", "encrypted_content": "opaque-latest"},
    ]


class ResponseAPI:
    """No socket: record synthetic outgoing bodies and return injected SSE frames."""

    def __init__(self, build: Callable[[int], list[dict[str, JsonValue]]]) -> None:
        """Bind a deterministic per-call event builder."""
        self.build = build
        self.requests: list[dict[str, JsonValue]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Serve only the fixed official Responses path through MockTransport."""
        assert str(request.url) == "https://api.openai.com/v1/responses"
        self.requests.append(_OBJECT.validate_json(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"".join(
                event_bytes(event) for event in self.build(len(self.requests))
            ),
        )


async def integrated(
    tmp_path: Path, api: ResponseAPI
) -> tuple[CheckpointStore, ContinuationCredential, ProviderClient]:
    """Generate account/session fixtures; do not use an existing account or network."""
    capability, service, settings, _, _, _, clock = await configured(tmp_path)
    service.settings.save(
        settings.model_copy(
            update={"maximum_requests_per_hour": None, "allow_function_tools": True}
        )
    )
    provider = capability.provider
    provider.transport = httpx.MockTransport(api.handle)
    store = CheckpointStore(
        provider.sessions.store,
        target_node=capability.target_node,
        callable_id=service.callable_id,
        descriptor_revision=service.revision,
        clock=clock,
        retention_seconds=300,
        maximum_records=32,
        maximum_storage_bytes=64 * 1024 * 1024,
    )
    credential = store.issue(models=("text-a",), expires_at=clock() + 3600)
    return store, credential, provider


async def collect(
    service: ContinuationService,
    store: CheckpointStore,
    credential: ContinuationCredential,
    messages: list[dict[str, JsonValue]],
    receipt: ContinuationReceipt | None = None,
    *,
    tools: bool = False,
) -> list[ContinuationEvent]:
    """Submit full public history through an actual client signature and HTTP sink."""
    proof, raw = signed(store, credential, messages, receipt=receipt)
    if tools:
        payload = _OBJECT.validate_json(raw)
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "caller_tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        raw = encode_body(payload)
        proof = credential.sign(
            proof.request.model_copy(update={"body": InputBody.from_bytes(raw)})
        )
    return [event async for event in service.generate(proof, raw)]


async def test_compaction_is_private_and_only_new_input_follows_latest_checkpoint(
    tmp_path: Path,
) -> None:
    api = ResponseAPI(
        lambda number: output_events(
            compacted_output() if number == 1 else [text_output()]
        )
    )
    store, credential, provider = await integrated(tmp_path, api)
    with store.worker() as worker:
        service = ContinuationService(worker, provider, profile=_PROFILE)
        first = await collect(service, store, credential, [USER])
        receipt = first[-1].receipt
        assert receipt is not None and first[-1].event.kind == "completed"
        assert first[0].event.delta == {"content": "hello"}
        assert all("opaque-" not in event.model_dump_json() for event in first)
        public: dict[str, JsonValue] = {"role": "assistant", "content": "hello"}
        following = await collect(
            service, store, credential, [USER, public, NEXT], receipt
        )
        assert following[-1].event.kind == "completed"
    assert api.requests[0]["context_management"] == [
        {"type": "compaction", "compact_threshold": 1024}
    ]
    items = api.requests[1]["input"]
    assert isinstance(items, list) and len(items) == 2
    assert items[0] == {
        "type": "compaction",
        "id": "cmp-2",
        "encrypted_content": "opaque-latest",
    }
    assert items[1] == {"role": "user", "content": "Continue"}
    assert (
        query(store, credential, following[-1].receipt or receipt)
        == following[-1].receipt
    )
    raw = store.credentials.path.read_bytes()
    assert b"opaque-latest" not in raw and b"Synthetic custody" not in raw


@pytest.mark.parametrize(
    "failure", ["eof", "trailing", "incomplete", "usage", "missing_done"]
)
@pytest.mark.parametrize("empty_snapshot", [False, True])
async def test_invalid_or_failed_stream_never_commits_receipt(
    tmp_path: Path, failure: str, empty_snapshot: bool
) -> None:
    def build(_number: int) -> list[dict[str, JsonValue]]:
        events = output_events(compacted_output())
        if empty_snapshot:
            response = events[-1]["response"]
            assert isinstance(response, dict)
            response["output"] = []
        if failure == "eof":
            return events[:-1]
        if failure == "trailing":
            return [*events, {"type": "response.output_text.delta", "delta": "late"}]
        if failure == "missing_done":
            return [
                event
                for event in events
                if event.get("type") != "response.output_item.done"
            ]
        return [
            *events[:-1],
            {
                "type": "response.incomplete"
                if failure == "incomplete"
                else "response.failed",
                "response": {
                    "error": {"code": "subscription_sharing_usage_limit_exceeded"}
                },
            },
        ]

    api = ResponseAPI(build)
    store, credential, provider = await integrated(tmp_path, api)
    profile = (
        _PROFILE.model_copy(update={"terminal_output": "empty_snapshot"})
        if empty_snapshot
        else _PROFILE
    )
    with store.worker() as worker:
        events = await collect(
            ContinuationService(worker, provider, profile=profile),
            store,
            credential,
            [USER],
        )
        assert events[-1].event.kind == "failed"
        assert all(event.receipt is None for event in events)
        assert not worker.leases
    with store.credentials.transaction() as connection:
        scope = connection.execute("SELECT scope FROM continuation_records").fetchone()[
            0
        ]
        record = store.load_record(connection, str(scope))
        assert record is not None and record.metadata.state.phase == "interrupted"
        assert not record.window and record.metadata.receipt is None
    assert provider.requests_started == 1


async def test_compaction_off_keeps_canonical_history_without_enabling_parameter(
    tmp_path: Path,
) -> None:
    api = ResponseAPI(lambda _: output_events([text_output()]))
    store, credential, provider = await integrated(tmp_path, api)
    with store.worker() as worker:
        service = ContinuationService(worker, provider)
        events = await collect(service, store, credential, [USER])
        assert events[-1].receipt is not None
    assert "context_management" not in api.requests[0]
    with store.credentials.transaction() as connection:
        scope = connection.execute("SELECT scope FROM continuation_records").fetchone()[
            0
        ]
        record = store.load_record(connection, str(scope))
        assert record is not None
        window = CanonicalWindow.decode(record.window)
        assert len(window.items) == 2 and window.profile_revision is None


async def test_unsolicited_compaction_is_refused_when_disabled(tmp_path: Path) -> None:
    api = ResponseAPI(lambda _: output_events(compacted_output()))
    store, credential, provider = await integrated(tmp_path, api)
    with store.worker() as worker:
        events = await collect(
            ContinuationService(worker, provider), store, credential, [USER]
        )
    assert events[-1].event.kind == "failed" and events[-1].receipt is None


@pytest.mark.parametrize(
    "change", ["missing", "empty", "omitted", "duplicated", "reordered"]
)
@pytest.mark.parametrize("empty_snapshot", [False, True])
async def test_terminal_output_must_equal_complete_ordered_capture(
    tmp_path: Path, change: str, empty_snapshot: bool
) -> None:
    def build(_: int) -> list[dict[str, JsonValue]]:
        events = output_events(compacted_output())
        response = events[-1]["response"]
        assert isinstance(response, dict)
        output = response["output"]
        assert isinstance(output, list)
        if change == "missing":
            del response["output"]
        else:
            response["output"] = (
                []
                if change == "empty"
                else output[:-1]
                if change == "omitted"
                else [*output, output[-1]]
                if change == "duplicated"
                else list(reversed(output))
            )
        return events

    api = ResponseAPI(build)
    store, credential, provider = await integrated(tmp_path, api)
    profile = (
        _PROFILE.model_copy(update={"terminal_output": "empty_snapshot"})
        if empty_snapshot
        else _PROFILE
    )
    with store.worker() as worker:
        events = await collect(
            ContinuationService(worker, provider, profile=profile),
            store,
            credential,
            [USER],
        )
        assert not worker.leases
    if change == "empty" and empty_snapshot:
        assert events[-1].event.kind == "completed" and events[-1].receipt is not None
    else:
        assert events[-1].event.kind == "failed"
        assert events[-1].event.error == "invalid_stream"
        assert all(event.receipt is None for event in events)


@pytest.mark.parametrize("mismatch", ["text", "tool_arguments", "item_identity"])
async def test_canonical_public_mismatch_never_commits(
    tmp_path: Path, mismatch: str
) -> None:
    def build(_: int) -> list[dict[str, JsonValue]]:
        if mismatch == "tool_arguments":
            events = output_events(
                [
                    {
                        "type": "function_call",
                        "id": "fc-1",
                        "call_id": "call-1",
                        "name": "caller_tool",
                        "arguments": "{}",
                        "status": "completed",
                    }
                ]
            )
            events[1]["delta"] = '{"different":true}'
        else:
            events = output_events([text_output()])
            if mismatch == "text":
                events[1]["delta"] = "different"
            else:
                item = events[0]["item"]
                assert isinstance(item, dict)
                item["id"] = "changed-before-done"
        return events

    api = ResponseAPI(build)
    store, credential, provider = await integrated(tmp_path, api)
    with store.worker() as worker:
        events = await collect(
            ContinuationService(worker, provider),
            store,
            credential,
            [USER],
            tools=mismatch == "tool_arguments",
        )
        assert events[-1].event.kind == "failed"
        assert not worker.leases
    assert all(event.receipt is None for event in events)


async def test_unsupplied_function_is_not_forwarded(tmp_path: Path) -> None:
    api = ResponseAPI(
        lambda _: output_events(
            [
                {
                    "type": "function_call",
                    "id": "fc-1",
                    "call_id": "call-1",
                    "name": "unrequested_tool",
                    "arguments": "{}",
                    "status": "completed",
                }
            ]
        )
    )
    store, credential, provider = await integrated(tmp_path, api)
    with store.worker() as worker:
        events = await collect(
            ContinuationService(worker, provider), store, credential, [USER], tools=True
        )
    assert len(events) == 1 and events[0].event.kind == "failed"
    assert "unrequested_tool" not in events[0].model_dump_json()


@pytest.mark.parametrize("empty_snapshot", [False, True])
async def test_duplicate_call_id_never_reaches_client_or_committed_history(
    tmp_path: Path, empty_snapshot: bool
) -> None:
    def build(_: int) -> list[dict[str, JsonValue]]:
        events = output_events(
            [
                {
                    "type": "function_call",
                    "id": f"fc-{index}",
                    "call_id": "duplicate",
                    "name": "caller_tool",
                    "arguments": "{}",
                    "status": "completed",
                }
                for index in (1, 2)
            ]
        )
        if empty_snapshot:
            response = events[-1]["response"]
            assert isinstance(response, dict)
            response["output"] = []
        return events

    api = ResponseAPI(build)
    store, credential, provider = await integrated(tmp_path, api)
    profile = (
        _PROFILE.model_copy(update={"terminal_output": "empty_snapshot"})
        if empty_snapshot
        else _PROFILE
    )
    with store.worker() as worker:
        events = await collect(
            ContinuationService(worker, provider, profile=profile),
            store,
            credential,
            [USER],
            tools=True,
        )
        assert not worker.leases
    assert events[-1].event.kind == "failed" and all(
        event.receipt is None for event in events
    )
    assert sum('"id":"duplicate"' in event.model_dump_json() for event in events) == 1
    with store.credentials.transaction() as connection:
        scope = str(
            connection.execute("SELECT scope FROM continuation_records").fetchone()[0]
        )
        record = store.load_record(connection, scope)
        assert (
            record is not None and not record.window and record.metadata.receipt is None
        )


async def test_revoked_client_cannot_publish_late_receipt(tmp_path: Path) -> None:
    def build(_: int) -> list[dict[str, JsonValue]]:
        store.revoke(credential.caller_id)
        return output_events([text_output()])

    api = ResponseAPI(build)
    store, credential, provider = await integrated(tmp_path, api)
    with store.worker() as worker:
        events = await collect(
            ContinuationService(worker, provider), store, credential, [USER]
        )
        assert events[-1].event.kind == "failed" and not worker.leases
    assert all(event.receipt is None for event in events)
    with store.credentials.transaction() as connection:
        assert (
            connection.execute("SELECT count(*) FROM continuation_records").fetchone()[
                0
            ]
            == 0
        )


class CancelledResponse(httpx.AsyncByteStream):
    """One visible delta followed by work that must close on caller cancellation."""

    closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Return enough canonical lifecycle to begin one response."""
        for event in output_events([text_output()])[:2]:
            yield event_bytes(event)
        raise AssertionError("cancelled response was resumed")

    async def aclose(self) -> None:
        """Record actual HTTP-response cleanup."""
        self.closed = True


async def test_cancelled_http_stream_closes_without_checkpoint(tmp_path: Path) -> None:
    api = ResponseAPI(lambda _: [])
    store, credential, provider = await integrated(tmp_path, api)
    stream = CancelledResponse()
    provider.transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream
        )
    )
    with store.worker() as worker:
        proof, raw = signed(store, credential, [USER])
        output = ContinuationService(worker, provider).generate(proof, raw)
        event = await anext(output)
        assert event.event.delta == {"content": "hello"}
        await output.aclose()
        assert stream.closed and not worker.leases
    with store.credentials.transaction() as connection:
        scope = str(
            connection.execute("SELECT scope FROM continuation_records").fetchone()[0]
        )
        record = store.load_record(connection, scope)
        assert record is not None and record.metadata.state.phase == "interrupted"
        assert not record.window and record.metadata.receipt is None


@pytest.mark.parametrize("cancellations", [1, 3])
async def test_cancellation_during_sql_admission_owns_eventual_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancellations: int
) -> None:
    api = ResponseAPI(lambda _: output_events([text_output()]))
    store, credential, provider = await integrated(tmp_path, api)
    entered, release = threading.Event(), threading.Event()
    with store.worker() as worker:
        original = worker.begin

        def blocked(proof: ContinuationProof, raw: bytes) -> CheckpointLease:
            entered.set()
            assert release.wait(3)
            return original(proof, raw)

        monkeypatch.setattr(worker, "begin", blocked)
        proof, raw = signed(store, credential, [USER])
        output = ContinuationService(worker, provider).generate(proof, raw)
        pending = asyncio.create_task(anext(output))
        assert await asyncio.to_thread(entered.wait, 3)
        try:
            for _ in range(cancellations):
                pending.cancel()
                await asyncio.sleep(0)
            assert not pending.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not worker.leases
    assert not api.requests
    with store.credentials.transaction() as connection:
        assert (
            connection.execute("SELECT count(*) FROM continuation_records").fetchone()[
                0
            ]
            == 0
        )


async def test_profile_change_requires_explicit_context_transition(
    tmp_path: Path,
) -> None:
    api = ResponseAPI(lambda _: output_events(compacted_output()))
    store, credential, provider = await integrated(tmp_path, api)
    with store.worker() as worker:
        first = await collect(
            ContinuationService(worker, provider, profile=_PROFILE),
            store,
            credential,
            [USER],
        )
        receipt = first[-1].receipt
        assert receipt is not None
        changed = _PROFILE.model_copy(update={"threshold": 2048})
        events = await collect(
            ContinuationService(worker, provider, profile=changed),
            store,
            credential,
            [USER, {"role": "assistant", "content": "hello"}, NEXT],
            receipt,
        )
    assert events[-1].continuation_error == "scope_mismatch"
    assert len(api.requests) == 1


async def test_exact_tool_history_is_validated_but_only_results_are_appended(
    tmp_path: Path,
) -> None:
    tool: dict[str, JsonValue] = {
        "type": "function_call",
        "id": "fc-1",
        "call_id": "call-1",
        "name": "caller_tool",
        "arguments": "{}",
        "status": "completed",
    }
    api = ResponseAPI(
        lambda number: output_events([tool] if number == 1 else [text_output()])
    )
    store, credential, provider = await integrated(tmp_path, api)
    public: dict[str, JsonValue] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "caller_tool", "arguments": "{}"},
            }
        ],
    }
    with store.worker() as worker:
        service = ContinuationService(worker, provider)
        first = await collect(service, store, credential, [USER], tools=True)
        receipt = first[-1].receipt
        assert receipt is not None and first[-1].event.finish_reason == "tool_calls"
        events = await collect(
            service,
            store,
            credential,
            [
                USER,
                public,
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "content": "Synthetic result",
                },
            ],
            receipt,
        )
    assert events[-1].event.kind == "completed"
    items = api.requests[1]["input"]
    assert isinstance(items, list)
    assert (
        sum(
            isinstance(item, dict) and item.get("type") == "function_call"
            for item in items
        )
        == 1
    )
    assert items[-1] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "Synthetic result",
    }


@pytest.mark.parametrize(
    "context,reserve,threshold",
    [(1000, 1, None), (10000, 9500, 1024), (10000, 1000, 9500)],
)
def test_model_headroom_cannot_be_guessed_or_overrun(
    context: int, reserve: int, threshold: int | None
) -> None:
    with pytest.raises(ValidationError):
        CompactionProfile(
            model="synthetic",
            context_tokens=context,
            reserved_output_tokens=reserve,
            threshold=threshold,
        )


def test_default_compaction_threshold_is_model_relative_and_reserves_output() -> None:
    profile = CompactionProfile(
        model="synthetic", context_tokens=65536, reserved_output_tokens=32768
    )
    assert profile.compact_threshold == 32768
