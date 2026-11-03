"""Document support must reflect model facts and be enforced before egress."""

import json
import secrets
from pathlib import Path

import pytest

from skulk_inference_reference.input_body import (
    InputBody,
    StreamedInvocation,
    StreamedProof,
    encode_body,
)
from skulk_openai.file_input import document_features
from tests.test_gateway_chat import configured
from tests.test_input_body import chunks, document


@pytest.mark.parametrize(
    "modalities,images,expected",
    [
        ((), True, ()),
        (("image",), True, ()),
        (("text",), True, ("text_file_input",)),
        (("text", "image"), False, ("text_file_input",)),
        (("text", "image"), True, ("text_file_input", "pdf_input")),
    ],
)
def test_document_projection_respects_missing_facts_and_image_permission(
    modalities: tuple[str, ...], images: bool, expected: tuple[str, ...]
) -> None:
    assert document_features(modalities, allow_images=images) == expected


@pytest.mark.parametrize(
    "modalities,images,name,accepted",
    [
        ((), True, "notes.txt", False),
        (("text",), False, "notes.txt", True),
        (("text",), True, "notes.pdf", False),
        (("text", "image"), False, "notes.pdf", False),
        (("text", "image"), True, "notes.pdf", True),
    ],
)
async def test_catalog_claims_and_actual_admission_match(
    tmp_path: Path, modalities: tuple[str, ...], images: bool, name: str, accepted: bool
) -> None:
    capability, service, settings, key, original, api, _ = await configured(tmp_path)
    cached = await capability.current_catalog()
    assert cached is not None
    capability.cache.save(
        cached.model_copy(
            update={
                "models": tuple(
                    model.model_copy(update={"input_modalities": modalities})
                    for model in cached.models
                )
            }
        )
    )
    service.settings.save(settings.model_copy(update={"allow_images": images}))
    legacy = await service.catalog()
    catalog = await service.streamed_catalog()
    assert legacy is not None and catalog is not None
    assert all(
        not {"text_file_input", "pdf_input"}.intersection(model.features)
        for model in legacy.models
    )
    model = next(model for model in catalog.models if model.id == "text-a")
    assert (
        ("pdf_input" if name.endswith(".pdf") else "text_file_input") in model.features
    ) is accepted
    raw = encode_body(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        document(
                            b"%PDF-1.4\n" if name.endswith(".pdf") else b"MARIGOLD",
                            name=name,
                        )
                    ],
                }
            ]
        }
    )
    invocation = StreamedInvocation(
        **original.invocation.model_dump(
            exclude={"contract", "request", "nonce", "catalog_revision"}
        ),
        catalog_revision=catalog.catalog_revision,
        body=InputBody.from_bytes(raw),
        nonce=secrets.token_hex(32),
    )
    proof = StreamedProof(
        invocation=invocation, signature=key.sign(invocation.canonical()).hex()
    )
    before = len(api.calls)
    events = [event async for event in service.generate_streamed(proof, chunks(raw))]
    assert events[-1].kind == ("completed" if accepted else "failed")
    assert capability.provider.requests_started == int(accepted)
    assert len(api.calls) == before + int(accepted)
    # The actual translated request retains the exact document shape, not text
    # flattening or a path/file-ID lookup.
    if accepted:
        assert (
            json.loads(api.calls[-1].content)["input"][0]["content"][0]["type"]
            == "input_file"
        )
