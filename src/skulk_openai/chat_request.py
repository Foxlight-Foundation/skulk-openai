"""Translate supported Chat Completions inputs into plan-usage Responses input."""

from typing import Literal

from pydantic import Field, JsonValue

from skulk_inference_reference.contracts import FrozenContract

from .settings import IntegrationSettings


class ChatRequest(FrozenContract):
    """Explicit supported controls; unsupported sampling fields are rejected."""

    messages: tuple[dict[str, JsonValue], ...] = Field(min_length=1, max_length=128)
    stream: bool = False
    stream_options: dict[str, JsonValue] | None = None
    tools: tuple[dict[str, JsonValue], ...] | None = Field(default=None, max_length=32)
    tool_choice: str | dict[str, JsonValue] | None = None
    parallel_tool_calls: bool | None = None
    response_format: dict[str, JsonValue] | None = None
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None
    ) = None
    enable_thinking: bool | None = None
    inference_policy: (
        Literal["allow_external", "local_only", "included_only"] | None
    ) = None

    def responses(self, settings: IntegrationSettings) -> dict[str, JsonValue]:
        """Build explicit history without passing unsupported preview parameters."""
        if self.inference_policy in {"local_only", "included_only"}:
            raise ValueError("hard policy excludes provider")
        items: list[JsonValue] = []
        if settings.instructions:
            items.append({"role": "developer", "content": settings.instructions})
        pending: set[str] = set()
        for message in self.messages:
            if set(message) - {
                "role",
                "content",
                "tool_calls",
                "tool_call_id",
                "name",
                "reasoning_content",
            }:
                raise ValueError("unsupported message fields")
            role, content = message.get("role"), message.get("content")
            if role == "tool":
                identifier = message.get("tool_call_id")
                if (
                    not isinstance(identifier, str)
                    or identifier not in pending
                    or not isinstance(content, str)
                ):
                    raise ValueError("unmatched tool result")
                pending.remove(identifier)
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": identifier,
                        "output": content,
                    }
                )
                continue
            if role not in ("system", "developer", "user", "assistant"):
                raise ValueError("unsupported message role")
            if pending:
                raise ValueError("missing tool results")
            # The preview rejects typed system items. Developer instructions
            # retain their intended priority without using that wire role.
            wire_role = "developer" if role == "system" else role
            if isinstance(content, str) and content:
                items.append({"role": wire_role, "content": content})
            elif isinstance(content, list):
                if role != "user":
                    raise ValueError("only user content supports images")
                parts: list[JsonValue] = []
                for part in content:
                    if not isinstance(part, dict):
                        raise ValueError("invalid content part")
                    if part.get("type") == "text" and isinstance(part.get("text"), str):
                        parts.append({"type": "input_text", "text": part["text"]})
                    elif part.get("type") == "image_url" and settings.allow_images:
                        image = part.get("image_url")
                        url = image.get("url") if isinstance(image, dict) else None
                        # Keep the capability's fixed-origin egress guarantee.
                        # Remote image fetching/SSRF is not delegated to us.
                        if not isinstance(url, str) or not url.startswith(
                            (
                                "data:image/png;base64,",
                                "data:image/jpeg;base64,",
                                "data:image/webp;base64,",
                            )
                        ):
                            raise ValueError("only inline images are supported")
                        parts.append({"type": "input_image", "image_url": url})
                    else:
                        raise ValueError("unsupported content part")
                if not parts:
                    raise ValueError("empty content")
                items.append({"role": wire_role, "content": parts})
            elif content is not None:
                raise ValueError("unsupported content")
            calls = message.get("tool_calls")
            if calls is not None:
                if (
                    role != "assistant"
                    or not settings.allow_function_tools
                    or not isinstance(calls, list)
                    or len(calls) > 32
                ):
                    raise ValueError("tool history is disabled or invalid")
                for call in calls:
                    if not isinstance(call, dict) or call.get("type") != "function":
                        raise ValueError("unsupported tool history")
                    function = call.get("function")
                    identifier = call.get("id")
                    if (
                        not isinstance(function, dict)
                        or not isinstance(identifier, str)
                        or identifier in pending
                    ):
                        raise ValueError("invalid function call")
                    name, arguments = function.get("name"), function.get("arguments")
                    if not isinstance(name, str) or not isinstance(arguments, str):
                        raise ValueError("invalid function")
                    pending.add(identifier)
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": identifier,
                            "name": name,
                            "arguments": arguments,
                        }
                    )
            elif content is None or content == "":
                raise ValueError("empty message")
        if pending:
            raise ValueError("missing tool results")
        result: dict[str, JsonValue] = {"input": items, "store": False, "stream": True}
        effort = self.reasoning_effort or settings.reasoning_effort
        if self.enable_thinking is False:
            effort = "none"
        elif self.enable_thinking is True and effort is None:
            effort = "medium"
        if effort is not None:
            result["reasoning"] = {"effort": effort, "summary": "auto"}
        text: dict[str, JsonValue] = {}
        if settings.verbosity is not None:
            text["verbosity"] = settings.verbosity
        if self.response_format is not None:
            format_type = self.response_format.get("type")
            if format_type == "json_schema":
                schema = self.response_format.get("json_schema")
                if (
                    not isinstance(schema, dict)
                    or not isinstance(schema.get("name"), str)
                    or not isinstance(schema.get("schema"), dict)
                ):
                    raise ValueError("invalid structured output schema")
                text["format"] = {"type": "json_schema", **schema}
            elif format_type == "json_object":
                text["format"] = {"type": "json_object"}
            elif format_type != "text":
                raise ValueError("unsupported response format")
        if text:
            result["text"] = text
        if self.tools:
            if not settings.allow_function_tools:
                raise ValueError("function tools are disabled")
            tools: list[JsonValue] = []
            for tool in self.tools:
                function = tool.get("function")
                if (
                    tool.get("type") != "function"
                    or not isinstance(function, dict)
                    or not isinstance(function.get("name"), str)
                ):
                    raise ValueError(
                        "only client-executed function tools are supported"
                    )
                if set(function) - {
                    "name",
                    "description",
                    "parameters",
                    "strict",
                    "id",
                }:
                    raise ValueError("unsupported function fields")
                tools.append(
                    {
                        "type": "function",
                        **{k: v for k, v in function.items() if k != "id"},
                    }
                )
            result["tools"] = tools
            if self.tool_choice is not None:
                choice = self.tool_choice
                if isinstance(choice, dict):
                    function = choice.get("function")
                    if (
                        choice.get("type") != "function"
                        or not isinstance(function, dict)
                        or not isinstance(function.get("name"), str)
                    ):
                        raise ValueError("invalid function selection")
                    choice = {"type": "function", "name": function["name"]}
                elif choice not in ("auto", "none", "required"):
                    raise ValueError("invalid tool choice")
                result["tool_choice"] = choice
            if self.parallel_tool_calls is not None:
                result["parallel_tool_calls"] = self.parallel_tool_calls
        elif (
            self.tool_choice not in (None, "none")
            or self.parallel_tool_calls is not None
        ):
            raise ValueError("tool options require tools")
        return result
