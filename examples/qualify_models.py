"""Probe installed managed models through Skulk's public unified API.

Planning is the default. Explicit execution sends synthetic inputs only, never
retries inference, and records serving facts separately from answer observations.
No provider credentials, settings, enrollment or private SDK interfaces are used.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import struct
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Literal, final
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

Object = dict[str, JsonValue]
_OBJECT = TypeAdapter(Object)
Profile = Literal["text_stream", "media_tools_schema"]
Outcome = Literal["not_run", "not_advertised", "submitted", "passed", "failed"]
FailureCode = Literal[
    "http_refusal", "transport_error", "deadline", "protocol_error", "response_bound"
]
MEDIA_FEATURES = frozenset(
    {"image_input", "text_file_input", "pdf_input", "function_tools"}
)
EXPECTED = {"color": "red", "text_word": "IRIS-COBALT", "pdf_word": "MARIGOLD"}
SCHEMA: Object = {
    "type": "object",
    "properties": {key: {"type": "string"} for key in EXPECTED},
    "required": list(EXPECTED),
    "additionalProperties": False,
}


class Frozen(BaseModel):
    """Strict immutable qualification data; unknown fields cannot alter semantics."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class _Wire(Frozen):
    # Public API responses may gain additive metadata without changing this probe.
    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")


class _Inference(_Wire):
    contract: str
    features: tuple[str, ...] = ()


class _Model(_Wire):
    id: str = Field(min_length=1, max_length=512)
    inference_capability: _Inference | None = None


class _Catalog(_Wire):
    data: tuple[_Model, ...] = Field(max_length=4096)


class _Descriptor(_Wire):
    qualified_id: str
    revision: str


class _Registration(_Wire):
    capabilities: tuple[_Descriptor, ...]


class _Release(_Wire):
    bundle_id: str
    bundle_version: str
    registration: _Registration


class _Service(_Wire):
    state: str
    active_digest: str | None = None


class _Installation(_Wire):
    release: _Release
    selected_digest: str | None = None
    enabled: bool
    stale: bool
    uninstalled: bool
    service: _Service


class _Inventory(_Wire):
    installations: tuple[_Installation, ...] = Field(max_length=1024)


class RuntimeIdentity(Frozen):
    """Observed installed identity, not independent signature verification."""

    bundle: str = Field(description="Provider's observed installation bundle ID.")
    version: str = Field(description="Observed capability bundle version.")
    digest: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="Selected active runtime."
    )
    qualified_id: str = Field(description="Advertised chat descriptor and version.")
    revision: str = Field(description="Observed immutable descriptor revision.")


class CaseResult(Frozen):
    """Content-free contract outcome for one model and synthetic workload."""

    model: str = Field(description="Concrete model ID, never a virtual route.")
    profile: Profile = Field(description="Exact synthetic serving workload.")
    runtime: RuntimeIdentity = Field(
        description="Installed identity observed before use."
    )
    outcome: Outcome = Field(
        default="not_run", description="No inference is implied by a plan."
    )
    submitted_requests: int = Field(
        default=0, ge=0, le=2, description="HTTP attempts, including failures."
    )
    elapsed_seconds: float = Field(
        default=0.0, ge=0, description="Whole workload duration."
    )
    failure: FailureCode | None = Field(
        default=None, description="Finite failure category; no provider error text."
    )
    text_characters: int = Field(
        default=0, ge=0, description="Returned public text count, without text."
    )
    synthetic_facts_match: bool | None = Field(
        default=None, description="Answer observation, separate from serving success."
    )
    tool_result_preserved: bool | None = Field(
        default=None,
        description="Whether final values equal the submitted tool result.",
    )


class Report(Frozen):
    """One explicit plan/execution receipt with no credentials or generated content."""

    schema_version: Literal[1] = Field(default=1, description="Receipt format version.")
    started_at: float = Field(ge=0, description="UTC epoch time at planning.")
    catalog_sha256: str = Field(description="Hash of selected catalog declarations.")
    harness_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="CLI example source hash; unknown for other callers.",
    )
    executed: bool = Field(
        default=False, description="Whether account-backed execution was requested."
    )
    complete: bool = Field(
        default=False, description="False for interrupted or in-progress runs."
    )
    runtime_stable: bool | None = Field(
        default=None, description="Observed identities matched after execution."
    )
    cases: tuple[CaseResult, ...] = Field(
        description="Per-workload facts; untested never means unsupported."
    )

    @property
    def passed(self) -> bool:
        """Require completion, stable identities and passing submitted cases."""
        return (
            self.executed
            and self.complete
            and self.runtime_stable is True
            and any(case.outcome == "passed" for case in self.cases)
            and all(case.outcome in {"passed", "not_advertised"} for case in self.cases)
        )


class ProbeFailure(Exception):
    """A finite local failure category that cannot echo provider-controlled text."""

    def __init__(self, code: FailureCode) -> None:
        """Retain the safe category for a content-free receipt."""
        super().__init__(code)
        self.code = code


@final
class StreamCheck:
    """Bounded SSE parser requiring semantic completion and clean transport EOF."""

    def __init__(self) -> None:
        """Create an empty parser without retaining model output."""
        self._buffer = bytearray()
        self._data: list[str] = []
        self._record_bytes = self._total = self.text_characters = 0
        self._finished = self._done = False

    def feed(self, part: bytes) -> None:
        """Consume bounded bytes; reject late errors/deltas and malformed records."""
        self._total += len(part)
        if self._total > 131072:
            raise ProbeFailure("response_bound")
        self._buffer.extend(part)
        while b"\n" in self._buffer:
            raw, _, remainder = self._buffer.partition(b"\n")
            self._buffer = bytearray(remainder)
            self._record_bytes += len(raw)
            if self._record_bytes > 32768:
                raise ProbeFailure("response_bound")
            line = raw.rstrip(b"\r").decode("utf-8")
            if not line:
                if self._data:
                    self._event("\n".join(self._data))
                self._data = []
                self._record_bytes = 0
            elif line.startswith("data:"):
                self._data.append(line[5:].removeprefix(" "))
        if len(self._buffer) + self._record_bytes > 32768:
            raise ProbeFailure("response_bound")

    def _event(self, data: str) -> None:
        if self._done:
            raise ProbeFailure("protocol_error")
        if data == "[DONE]":
            if not self._finished or not self.text_characters:
                raise ProbeFailure("protocol_error")
            self._done = True
            return
        event = _OBJECT.validate_json(data)
        if event.get("error") is not None:
            raise ProbeFailure("protocol_error")
        choices = event.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise ProbeFailure("protocol_error")
        for item in choices:
            if not isinstance(item, dict) or item.get("index", 0) != 0:
                raise ProbeFailure("protocol_error")
            delta = item.get("delta", {})
            if (
                not isinstance(delta, dict)
                or delta.get("tool_calls")
                or delta.get("role", "assistant") != "assistant"
            ):
                raise ProbeFailure("protocol_error")
            text = delta.get("content") or ""
            summary = delta.get("reasoning") or delta.get("reasoning_content") or ""
            if not isinstance(text, str) or not isinstance(summary, str):
                raise ProbeFailure("protocol_error")
            if self._finished and (text or summary):
                raise ProbeFailure("protocol_error")
            self.text_characters += len(text)
            finish = item.get("finish_reason")
            if finish is not None:
                if self._finished or finish != "stop":
                    raise ProbeFailure("protocol_error")
                self._finished = True

    def complete(self) -> int:
        """Accept EOF only after a complete DONE record; return public text count."""
        if self._buffer or self._data or not self._done:
            raise ProbeFailure("protocol_error")
        return self.text_characters


def _runtime(model: _Model, inventory: _Inventory) -> RuntimeIdentity:
    if model.inference_capability is None:
        raise ValueError("Only managed models are eligible")
    parts = model.id.split("/", 2)
    if len(parts) != 3 or parts[0] != "capability":
        raise ValueError("A concrete managed model is required")
    version = model.inference_capability.contract.rsplit("@", 1)[-1]
    qualified = parts[1] + "@" + version
    matches: list[RuntimeIdentity] = []
    for installation in inventory.installations:
        if (
            not installation.enabled
            or installation.stale
            or installation.uninstalled
            or installation.service.state != "running"
            or installation.selected_digest is None
            or installation.service.active_digest != installation.selected_digest
        ):
            continue
        for descriptor in installation.release.registration.capabilities:
            if descriptor.qualified_id == qualified:
                matches.append(
                    RuntimeIdentity(
                        bundle=installation.release.bundle_id,
                        version=installation.release.bundle_version,
                        digest=installation.selected_digest,
                        qualified_id=qualified,
                        revision=descriptor.revision,
                    )
                )
    if len(matches) != 1:
        raise ValueError("No unique ready installed descriptor")
    return matches[0]


def plan(
    catalog: bytes,
    inventory: bytes,
    models: tuple[str, ...],
    *,
    all_enabled: bool,
    media: bool,
    now: float,
) -> Report:
    """Plan concrete managed models; unknown IDs never trigger inference."""
    listing = _Catalog.model_validate_json(catalog)
    installed = _Inventory.model_validate_json(inventory)
    available = {
        model.id: model for model in listing.data if model.inference_capability
    }
    if len(available) != sum(
        model.inference_capability is not None for model in listing.data
    ):
        raise ValueError("Duplicate model identities")
    if all_enabled and models:
        raise ValueError("Choose explicit models or all enabled models")
    selected = tuple(sorted(available)) if all_enabled or not models else models
    if len(selected) > 128 or len(set(selected)) != len(selected):
        raise ValueError("Models must be bounded and unique")
    if any(model not in available for model in selected):
        raise ValueError("A requested model is unavailable")
    cases: list[CaseResult] = []
    declarations: list[JsonValue] = []
    for identity in selected:
        model = available[identity]
        runtime = _runtime(model, installed)
        cases.append(CaseResult(model=identity, profile="text_stream", runtime=runtime))
        assert model.inference_capability is not None
        declarations.append(_OBJECT.validate_json(model.model_dump_json()))
        if media:
            enabled = MEDIA_FEATURES <= set(model.inference_capability.features)
            cases.append(
                CaseResult(
                    model=identity,
                    profile="media_tools_schema",
                    runtime=runtime,
                    outcome="not_run" if enabled else "not_advertised",
                )
            )
    return Report(
        started_at=now,
        catalog_sha256=hashlib.sha256(
            json.dumps(declarations, sort_keys=True).encode()
        ).hexdigest(),
        cases=tuple(cases),
    )


def fixture_content() -> list[JsonValue]:
    """Return self-contained synthetic PNG, UTF-8 and PDF input; no local files."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    image = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0" * 32) * 32))
        + chunk(b"IEND", b"")
    )
    stream = b"BT /F1 16 Tf 72 720 Td (Verification word: MARIGOLD) Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"endstream",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(str(index).encode() + b" 0 obj\n" + value + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref).encode()
        + b"\n%%EOF\n"
    )

    def file(name: str, mime: str, data: bytes) -> JsonValue:
        return {
            "type": "file",
            "file": {
                "filename": name,
                "file_data": "data:"
                + mime
                + ";base64,"
                + base64.b64encode(data).decode(),
            },
        }

    return [
        {
            "type": "text",
            "text": (
                "Extract the verification word from EACH attached document. "
                "Call return_fixture with the image color, text_word from "
                "the UTF-8 document and pdf_word from the PDF. "
                "Use the exact words in the attachments."
            ),
        },
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(image).decode()
            },
        },
        file("word.txt", "text/plain", b"Verification word: IRIS-COBALT"),
        file("verification.pdf", "application/pdf", bytes(pdf)),
    ]


def _message(value: Object, finish: str) -> Object:
    if value.get("error") is not None:
        raise ProbeFailure("protocol_error")
    choices = value.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ProbeFailure("protocol_error")
    choice = choices[0]
    if (
        not isinstance(choice, dict)
        or choice.get("finish_reason") != finish
        or not isinstance(choice.get("message"), dict)
    ):
        raise ProbeFailure("protocol_error")
    message = choice["message"]
    assert isinstance(message, dict)
    if message.get("role") != "assistant":
        raise ProbeFailure("protocol_error")
    return message


def _facts(value: Object) -> dict[str, str]:
    if set(value) != set(EXPECTED) or not all(
        isinstance(item, str) for item in value.values()
    ):
        raise ProbeFailure("protocol_error")
    return {key: str(item) for key, item in value.items()}


@final
class _Progress:
    """Publish immutable evidence before egress without late-bound loop callbacks."""

    def __init__(self, report: Report, publish: Callable[[Report], None]) -> None:
        self.report, self.publish = report, publish
        publish(report)

    def update(self, position: int, **changes: object) -> None:
        cases = list(self.report.cases)
        cases[position] = cases[position].model_copy(update=changes)
        self.report = self.report.model_copy(update={"cases": tuple(cases)})
        self.publish(self.report)

    def submitted(self, position: int) -> None:
        self.update(
            position,
            outcome="submitted",
            submitted_requests=self.report.cases[position].submitted_requests + 1,
        )


@final
class Qualification:
    """Bounded public-API probes using an injected HTTP transport and receipt sink."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        publish: Callable[[Report], None],
        *,
        deadline_seconds: float = 120.0,
    ) -> None:
        """Retain client/sink; the caller owns cleanup and metadata authority."""
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("A finite positive probe deadline is required")
        self._client, self._publish = client, publish
        self._deadline = deadline_seconds

    async def read(self, path: Literal["/v1/plugins/managed", "/v1/models"]) -> bytes:
        """Read bounded public metadata without changing owner settings."""
        async with asyncio.timeout(self._deadline):
            async with self._client.stream("GET", path) as response:
                if response.status_code != 200:
                    raise ProbeFailure("http_refusal")
                return await self._bytes(
                    response, 16777216 if path == "/v1/models" else 1048576
                )

    async def _bytes(self, response: httpx.Response, limit: int) -> bytes:
        data = bytearray()
        async for part in response.aiter_bytes(chunk_size=8192):
            data.extend(part)
            if len(data) > limit:
                raise ProbeFailure("response_bound")
        return bytes(data)

    async def _json(self, body: Object) -> Object:
        async with self._client.stream(
            "POST", "/v1/chat/completions", json=body
        ) as response:
            if response.status_code != 200:
                raise ProbeFailure("http_refusal")
            return _OBJECT.validate_json(await self._bytes(response, 262144))

    async def _stream(self, model: str) -> int:
        body: Object = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with one short greeting."}],
            "stream": True,
        }
        async with self._client.stream(
            "POST", "/v1/chat/completions", json=body
        ) as response:
            if response.status_code != 200:
                raise ProbeFailure("http_refusal")
            if not response.headers.get("content-type", "").startswith(
                "text/event-stream"
            ):
                raise ProbeFailure("protocol_error")
            parser = StreamCheck()
            async for part in response.aiter_bytes(chunk_size=8192):
                parser.feed(part)
        return parser.complete()

    async def execute(self, report: Report) -> Report:
        """Submit planned cases once, preserving failure facts without retries."""
        if (
            report.executed
            or report.complete
            or any(
                case.outcome not in {"not_run", "not_advertised"}
                for case in report.cases
            )
        ):
            raise ValueError("Only a fresh plan can execute")
        progress = _Progress(
            report.model_copy(update={"executed": True}), self._publish
        )
        for index, case in enumerate(progress.report.cases):
            if case.outcome == "not_advertised":
                continue
            started = time.monotonic()
            try:
                async with asyncio.timeout(self._deadline):
                    if case.profile == "text_stream":
                        progress.submitted(index)
                        count = await self._stream(case.model)
                        progress.update(index, outcome="passed", text_characters=count)
                    else:
                        history: list[JsonValue] = [
                            {"role": "user", "content": fixture_content()}
                        ]
                        tool: JsonValue = {
                            "type": "function",
                            "function": {
                                "name": "return_fixture",
                                "description": "Return the synthetic fixture facts.",
                                "parameters": SCHEMA,
                            },
                        }
                        progress.submitted(index)
                        first = await self._json(
                            {
                                "model": case.model,
                                "messages": history,
                                "stream": False,
                                "tools": [tool],
                                "tool_choice": {
                                    "type": "function",
                                    "function": {"name": "return_fixture"},
                                },
                            }
                        )
                        message = _message(first, "tool_calls")
                        calls = message.get("tool_calls")
                        if (
                            not isinstance(calls, list)
                            or len(calls) != 1
                            or not isinstance(calls[0], dict)
                        ):
                            raise ProbeFailure("protocol_error")
                        call = calls[0]
                        function, identity = call.get("function"), call.get("id")
                        if (
                            call.get("type") != "function"
                            or not isinstance(function, dict)
                            or function.get("name") != "return_fixture"
                            or not isinstance(identity, str)
                            or not identity
                            or len(identity) > 256
                        ):
                            raise ProbeFailure("protocol_error")
                        arguments = function.get("arguments")
                        if not isinstance(arguments, str) or len(arguments) > 8192:
                            raise ProbeFailure("protocol_error")
                        values = _facts(_OBJECT.validate_json(arguments))
                        match = values["color"].lower() == "red" and all(
                            values[key] == EXPECTED[key]
                            for key in ("text_word", "pdf_word")
                        )
                        progress.update(index, synthetic_facts_match=match)
                        # Forward the public assistant/tool fields only. Additive
                        # response metadata cannot become input instructions.
                        assistant: Object = {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": identity,
                                    "type": "function",
                                    "function": {
                                        "name": "return_fixture",
                                        "arguments": arguments,
                                    },
                                }
                            ],
                        }
                        assistant_content = message.get("content")
                        if assistant_content is not None:
                            if not isinstance(assistant_content, str):
                                raise ProbeFailure("protocol_error")
                            assistant["content"] = assistant_content
                        history.extend(
                            [
                                assistant,
                                {
                                    "role": "tool",
                                    "tool_call_id": identity,
                                    "content": json.dumps(values),
                                },
                                {
                                    "role": "user",
                                    "content": (
                                        "Return the same three values from the "
                                        "tool result as a JSON "
                                        "object. Do not call a tool again."
                                    ),
                                },
                            ]
                        )
                        progress.submitted(index)
                        second = await self._json(
                            {
                                "model": case.model,
                                "messages": history,
                                "stream": False,
                                "tools": [tool],
                                "tool_choice": "none",
                                "response_format": {
                                    "type": "json_schema",
                                    "json_schema": {
                                        "name": "fixture_result",
                                        "strict": True,
                                        "schema": SCHEMA,
                                    },
                                },
                            }
                        )
                        reply = _message(second, "stop")
                        content = reply.get("content")
                        if not isinstance(content, str) or reply.get("tool_calls"):
                            raise ProbeFailure("protocol_error")
                        facts = _facts(_OBJECT.validate_json(content))
                        progress.update(
                            index,
                            outcome="passed",
                            tool_result_preserved=facts == values,
                        )
            except ProbeFailure as error:
                progress.update(index, outcome="failed", failure=error.code)
            except TimeoutError:
                progress.update(index, outcome="failed", failure="deadline")
            except httpx.HTTPError:
                progress.update(index, outcome="failed", failure="transport_error")
            except ValueError:
                progress.update(index, outcome="failed", failure="protocol_error")
            finally:
                progress.update(
                    index, elapsed_seconds=round(time.monotonic() - started, 3)
                )
        final_inventory = _Inventory.model_validate_json(
            await self.read("/v1/plugins/managed")
        )
        # A replacement during the run invalidates identity-bound aggregate claims.
        observed = tuple(case.runtime for case in progress.report.cases)
        active = tuple(
            RuntimeIdentity(
                bundle=item.release.bundle_id,
                version=item.release.bundle_version,
                digest=item.selected_digest,
                qualified_id=descriptor.qualified_id,
                revision=descriptor.revision,
            )
            for item in final_inventory.installations
            if item.enabled
            and not item.stale
            and not item.uninstalled
            and item.service.state == "running"
            and item.selected_digest is not None
            and item.selected_digest == item.service.active_digest
            for descriptor in item.release.registration.capabilities
        )
        current = progress.report.model_copy(
            update={
                "complete": True,
                "runtime_stable": all(identity in active for identity in observed),
            }
        )
        self._publish(current)
        return current


def _origin(value: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise ValueError("Use an HTTP(S) origin without credentials, path or query")
    return value.rstrip("/")


async def _main(arguments: argparse.Namespace) -> int:
    base = _origin(str(arguments.base_url))
    models = tuple(str(value) for value in arguments.model)
    execute, all_enabled, media = (
        bool(arguments.execute),
        bool(arguments.all_enabled),
        bool(arguments.media),
    )
    destination = Path(str(arguments.report)) if arguments.report else None
    if execute and (not models and not all_enabled or destination is None):
        raise ValueError(
            "Execution requires explicit models/all-enabled and a new report path"
        )
    if destination is not None:
        with os.fdopen(
            os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
            "w",
            encoding="utf-8",
        ) as output:
            _ = output.write('{"complete":false}\n')
        destination.chmod(0o600)

    def publish(report: Report) -> None:
        if destination is not None:
            # Replace one task-owned report; never overwrite an existing user file.
            temporary = destination.with_name(destination.name + ".tmp")
            with os.fdopen(
                os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
                "w",
                encoding="utf-8",
            ) as output:
                _ = output.write(report.model_dump_json(indent=2) + "\n")
            temporary.chmod(0o600)
            os.replace(temporary, destination)

    async with httpx.AsyncClient(
        base_url=base,
        headers={"Origin": base, "X-Skulk-Dashboard": "pairing-v1"},
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(120.0, connect=5.0),
    ) as client:
        runner = Qualification(client, publish)
        inventory = await runner.read("/v1/plugins/managed")
        catalog = await runner.read("/v1/models")
        report = plan(
            catalog,
            inventory,
            models,
            all_enabled=all_enabled,
            media=media,
            now=time.time(),
        )
        source = await asyncio.to_thread(Path(__file__).read_bytes)
        report = report.model_copy(
            update={"harness_sha256": hashlib.sha256(source).hexdigest()}
        )
        publish(report)
        if execute:
            report = await runner.execute(report)
        print(report.model_dump_json(indent=2))
        return int(execute and not report.passed)


def main() -> int:
    """Parse options; default to metadata-only planning and safe errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--base-url", default="http://127.0.0.1:52415")
    _ = parser.add_argument("--model", action="append", default=[])
    _ = parser.add_argument("--all-enabled", action="store_true")
    _ = parser.add_argument(
        "--media",
        action="store_true",
        help="Add PNG/text/PDF, a tool round trip and JSON schema.",
    )
    _ = parser.add_argument(
        "--execute",
        action="store_true",
        help="Use the connected account for the planned synthetic requests.",
    )
    _ = parser.add_argument(
        "--report", help="New content-free receipt path; required for execution."
    )
    try:
        return asyncio.run(_main(parser.parse_args()))
    except (ValueError, OSError, httpx.HTTPError, TimeoutError, ProbeFailure) as error:
        print(
            json.dumps(
                {
                    "complete": False,
                    "failure_class": type(error).__name__,
                    "failure": error.code if isinstance(error, ProbeFailure) else None,
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
