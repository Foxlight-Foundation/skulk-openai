"""Bounded media input must verify before any provider can receive its contents."""

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from skulk_inference_reference.input_body import (
    MAX_BODY_BYTES,
    MAX_BODY_CHUNK_BYTES,
    InputBody,
    StreamedInvocation,
    StreamedProof,
    encode_body,
    receive_body,
)
from skulk_openai.chat_request import ChatRequest
from skulk_openai.file_input import MAX_FILE_BYTES, responses_file
from skulk_openai.settings import IntegrationSettings
from tests.test_gateway_chat import configured


async def chunks(raw: bytes) -> AsyncIterator[bytes]:
    """Split synthetic media into legal SDK-sized packets."""
    for start in range(0, len(raw), MAX_BODY_CHUNK_BYTES):
        yield raw[start : start + MAX_BODY_CHUNK_BYTES]


def document(
    raw: bytes = b"Synthetic document", *, name: str = "example.txt"
) -> dict[str, JsonValue]:
    """Build inline client document data without accessing the filesystem."""
    media = "application/pdf" if name.endswith(".pdf") else "text/plain"
    return {
        "type": "file",
        "file": {
            "filename": name,
            "file_data": "data:" + media + ";base64," + base64.b64encode(raw).decode(),
        },
    }


def payload(raw: bytes = b"x" * 200_000) -> dict[str, JsonValue]:
    """Use a practical-size file, larger than the old control envelope."""
    return {"messages": [{"role": "user", "content": [document(raw)]}]}


async def test_body_roundtrip_is_provider_neutral_and_beyond_control_limit() -> None:
    request = payload(b"x" * 1_100_000)
    raw = encode_body(request)
    reference = InputBody.from_bytes(raw)
    assert reference.size_bytes > MAX_BODY_CHUNK_BYTES
    assert await receive_body(reference, chunks(raw)) == request
    assert "Synthetic" not in reference.model_dump_json()
    assert len(reference.model_dump_json()) < 256


@pytest.mark.parametrize("change", ["short", "long", "digest", "empty", "packet"])
async def test_invalid_media_never_becomes_a_request(change: str) -> None:
    raw = encode_body(payload())
    reference = InputBody.from_bytes(raw)
    if change == "short":
        raw = raw[:-1]
    elif change == "long":
        raw += b" "
    elif change == "digest":
        raw = raw.replace(b"user", b"tool", 1)
    elif change == "packet":
        raw = b"x" * (MAX_BODY_CHUNK_BYTES + 1)
        reference = InputBody.from_bytes(raw)

    async def bad() -> AsyncIterator[bytes]:
        yield b"" if change == "empty" else raw

    with pytest.raises(ValueError):
        await receive_body(reference, bad())


@pytest.mark.parametrize(
    "raw",
    [b'{"messages":[],"messages":[]}', b"[]", b"\xff\xff", b"{}" + b"x"],
)
async def test_ambiguous_or_invalid_json_is_refused(raw: bytes) -> None:
    with pytest.raises(ValueError):
        await receive_body(InputBody.from_bytes(raw), chunks(raw))


def test_transfer_size_is_an_explicit_bound() -> None:
    with pytest.raises(ValidationError):
        InputBody(size_bytes=MAX_BODY_BYTES + 1, sha256="a" * 64)


@pytest.mark.parametrize(
    "name,raw", [("example.txt", b"Hello"), ("example.pdf", b"%PDF-1.4\n")]
)
def test_file_translation_preserves_exact_bytes(name: str, raw: bytes) -> None:
    part = document(raw, name=name)
    translated = responses_file(part)
    assert translated["type"] == "input_file"
    assert translated["filename"] == name
    request = ChatRequest.model_validate_json(json.dumps(payload(raw)))
    items = request.responses(IntegrationSettings())["input"]
    assert isinstance(items, list) and isinstance(items[0], dict)
    assert items[0]["role"] == "user"


@pytest.mark.parametrize(
    "file",
    [
        {"filename": "../example.txt", "file_data": "data:text/plain;base64,eA=="},
        {"filename": "example.txt", "file_id": "provider-file"},
        {"filename": "example.txt", "file_data": "https://example.com/file"},
        {"filename": "example.pdf", "file_data": "data:text/plain;base64,eA=="},
        {"filename": "example.zip", "file_data": "data:application/zip;base64,eA=="},
        {"filename": "example.txt", "file_data": "data:text/plain;base64,eA=\n="},
        {"filename": "example.txt", "file_data": "data:text/plain;base64,eB=="},
        {"filename": "example.txt", "file_data": "data:text/plain;base64,/w=="},
        {"filename": "example.txt", "file_data": "data:text/plain;base64,AA=="},
        {"filename": "example.pdf", "file_data": "data:application/pdf;base64,eA=="},
    ],
)
def test_untrusted_file_references_and_malformed_data_are_refused(
    file: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValueError):
        responses_file({"type": "file", "file": file})


def test_document_count_and_decoded_byte_bounds() -> None:
    with pytest.raises(ValueError):
        responses_file(document(b"x" * (MAX_FILE_BYTES + 1)))
    request = ChatRequest.model_validate_json(
        json.dumps({"messages": [{"role": "user", "content": [document()] * 9}]})
    )
    with pytest.raises(ValueError):
        request.responses(IntegrationSettings())


async def test_signed_stream_admission_binds_documents_and_replay(
    tmp_path: Path,
) -> None:
    capability, service, _, key, original, api, _ = await configured(tmp_path)
    raw = encode_body(payload())
    invocation = StreamedInvocation(
        **original.invocation.model_dump(exclude={"contract", "request"}),
        body=InputBody.from_bytes(raw),
    )
    proof = StreamedProof(
        invocation=invocation, signature=key.sign(invocation.canonical()).hex()
    )
    events = [event async for event in service.generate_streamed(proof, chunks(raw))]
    assert events[-1].kind == "completed" and capability.provider.requests_started == 1
    upstream = json.loads(api.calls[-1].content)
    assert upstream["input"][0]["content"][0]["type"] == "input_file"
    assert "body" not in upstream
    assert [e.error async for e in service.generate_streamed(proof, chunks(raw))] == [
        "policy_denied"
    ]
    assert capability.provider.requests_started == 1


@pytest.mark.parametrize(
    "denial", ["tamper", "signature", "revoke", "expiry", "cancel"]
)
async def test_streaming_denials_keep_all_content_local(
    tmp_path: Path, denial: str
) -> None:
    capability, service, settings, key, original, _, clock = await configured(tmp_path)
    raw = encode_body(payload())
    invocation = StreamedInvocation(
        **original.invocation.model_dump(exclude={"contract", "request", "nonce"}),
        body=InputBody.from_bytes(raw),
        nonce=secrets.token_hex(32),
    )
    proof = StreamedProof(
        invocation=invocation,
        signature="0" * 128
        if denial == "signature"
        else key.sign(invocation.canonical()).hex(),
    )
    consumed = False

    async def upload() -> AsyncIterator[bytes]:
        nonlocal consumed
        consumed = True
        yield raw[:-1]
        if denial == "revoke":
            service.settings.save(
                settings.model_copy(update={"processing": "local_only"})
            )
        elif denial == "expiry":
            clock.now += 31
        elif denial == "cancel":
            raise asyncio.CancelledError
        yield b"x" if denial == "tamper" else raw[-1:]

    if denial == "cancel":
        with pytest.raises(asyncio.CancelledError):
            _ = [event async for event in service.generate_streamed(proof, upload())]
    else:
        events = [event async for event in service.generate_streamed(proof, upload())]
        assert events[-1].kind == "failed"
    assert capability.provider.requests_started == 0
    assert consumed is (denial != "signature")
    assert hashlib.sha256(
        raw
    ).hexdigest() not in capability.grants.credentials.path.read_bytes().decode(
        errors="ignore"
    )
