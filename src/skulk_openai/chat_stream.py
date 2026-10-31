"""Bounded Responses-to-chat adaptation, including code text and function calls."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing

import httpx
from pydantic import JsonValue

from skulk_inference_reference.chat import ChatEvent

from .responses import InvalidResponseStream, server_events


async def chat_events(
    response: httpx.Response,
    *,
    maximum_characters: int,
) -> AsyncGenerator[ChatEvent]:
    """Preserve explicit failure, function deltas, reasoning summaries and usage.

    No tools execute here. Only caller-supplied functions can be returned for
    the client to execute, using its own separate permissions.
    """
    terminal: ChatEvent | None = None
    characters = 0
    tools: dict[str, int] = {}
    try:
        if response.status_code != 200:
            yield ChatEvent(kind="failed", error="upstream_failed")
            return
        media = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if media and media != "text/event-stream":
            raise ValueError("invalid media")
        deadline = asyncio.get_running_loop().time() + 120
        async with aclosing(
            server_events(
                response,
                maximum_event_bytes=524288,
                maximum_stream_bytes=2097152,
                maximum_events=8192,
            )
        ) as events:
            while True:
                try:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError
                    event = await asyncio.wait_for(
                        anext(events), min(remaining, 0.5) if terminal else remaining
                    )
                except StopAsyncIteration:
                    break
                if terminal is not None:
                    raise ValueError("event after terminal")
                kind = event.get("type")
                delta: dict[str, JsonValue] = {}
                if kind in (
                    "response.output_text.delta",
                    "response.reasoning_summary_text.delta",
                    "response.refusal.delta",
                ):
                    text = event.get("delta")
                    if not isinstance(text, str) or not text:
                        raise ValueError("invalid delta")
                    characters += len(text)
                    delta[
                        "reasoning_content"
                        if kind == "response.reasoning_summary_text.delta"
                        else "content"
                    ] = text
                elif kind == "response.output_item.added":
                    item = event.get("item")
                    if not isinstance(item, dict):
                        raise ValueError("missing item")
                    if item.get("type") == "function_call":
                        identifier, call_id, name = (
                            item.get("id"),
                            item.get("call_id"),
                            item.get("name"),
                        )
                        arguments = item.get("arguments", "")
                        if (
                            not isinstance(identifier, str)
                            or identifier in tools
                            or len(tools) >= 32
                            or not isinstance(call_id, str)
                            or not isinstance(name, str)
                            or not isinstance(arguments, str)
                        ):
                            raise ValueError("invalid tool")
                        tools[identifier] = len(tools)
                        characters += len(arguments) + len(name)
                        delta["tool_calls"] = [
                            {
                                "index": tools[identifier],
                                "id": call_id,
                                "type": "function",
                                "function": {"name": name, "arguments": arguments},
                            }
                        ]
                    elif item.get("type") not in ("message", "reasoning"):
                        raise ValueError("unsupported tool output")
                elif kind == "response.function_call_arguments.delta":
                    identifier, arguments = event.get("item_id"), event.get("delta")
                    if (
                        not isinstance(identifier, str)
                        or identifier not in tools
                        or not isinstance(arguments, str)
                    ):
                        raise ValueError("unknown tool delta")
                    characters += len(arguments)
                    delta["tool_calls"] = [
                        {
                            "index": tools[identifier],
                            "function": {"arguments": arguments},
                        }
                    ]
                elif kind == "response.completed":
                    value = event.get("response")
                    raw_usage = value.get("usage") if isinstance(value, dict) else None
                    usage: dict[str, int] | None = None
                    if isinstance(raw_usage, dict):
                        usage = {}
                        for source, target in (
                            ("input_tokens", "prompt_tokens"),
                            ("output_tokens", "completion_tokens"),
                            ("total_tokens", "total_tokens"),
                        ):
                            count = raw_usage.get(source)
                            if type(count) is int and 0 <= count <= 2**53:
                                usage[target] = count
                    terminal = ChatEvent(
                        kind="completed",
                        finish_reason="tool_calls" if tools else "stop",
                        usage=usage,
                    )
                elif kind in ("response.failed", "response.incomplete", "error"):
                    value = event.get("response")
                    failure = value.get("error") if isinstance(value, dict) else event
                    code = failure.get("code") if isinstance(failure, dict) else None
                    error = (
                        "usage_limit"
                        if code == "subscription_sharing_usage_limit_exceeded"
                        else "usage_unavailable"
                        if code == "subscription_sharing_usage_unavailable"
                        else "incomplete"
                        if kind == "response.incomplete"
                        else "upstream_failed"
                    )
                    terminal = ChatEvent(kind="failed", error=error)
                elif kind not in (
                    "response.created",
                    "response.in_progress",
                    "response.metadata",
                    "response.content_part.added",
                    "response.content_part.done",
                    "response.output_item.done",
                    "response.output_text.done",
                    "response.function_call_arguments.done",
                    "response.refusal.done",
                    "response.reasoning_summary_part.added",
                    "response.reasoning_summary_part.done",
                    "response.reasoning_summary_text.done",
                ):
                    raise ValueError("unsupported event")
                if characters > maximum_characters:
                    yield ChatEvent(kind="failed", error="output_limit")
                    return
                if delta:
                    yield ChatEvent(kind="delta", delta=delta)
        yield terminal or ChatEvent(kind="failed", error="interrupted")
    except (InvalidResponseStream, ValueError):
        yield ChatEvent(kind="failed", error="invalid_stream")
    except (httpx.HTTPError, TimeoutError):
        yield (
            terminal
            if terminal is not None and terminal.kind == "failed"
            else ChatEvent(kind="failed", error="interrupted")
        )
