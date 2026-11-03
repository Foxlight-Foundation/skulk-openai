"""Validate inline document inputs without reading paths or fetching URLs."""

import base64
import binascii
import re
from typing import Literal

from pydantic import JsonValue

MAX_FILE_BYTES = 8 * 1024 * 1024
_NAME = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,128}$")


def document_features(
    input_modalities: tuple[str, ...], *, allow_images: bool
) -> tuple[Literal["text_file_input", "pdf_input"], ...]:
    """Project implemented Responses file inputs from declared model modalities.

    PDF processing includes page images, so it also respects image permission.
    Missing modality facts never establish document support.
    """
    if "text" not in input_modalities:
        return ()
    return (
        "text_file_input",
        *(("pdf_input",) if allow_images and "image" in input_modalities else ()),
    )


def validate_document_features(
    body: dict[str, JsonValue],
    features: tuple[Literal["text_file_input", "pdf_input"], ...],
) -> None:
    """Refuse unsupported documents before reserving usage or contacting upstream."""
    items = body.get("input")
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "input_file":
                continue
            filename = part.get("filename")
            required = (
                "pdf_input"
                if isinstance(filename, str) and filename.lower().endswith(".pdf")
                else "text_file_input"
            )
            if required not in features:
                raise ValueError("selected model does not support this document input")


def responses_file(part: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Translate one inline UTF-8 text/PDF document to Responses input_file.

    Filenames are display metadata only. Remote references, provider file IDs,
    local paths and malformed or mismatched data URLs fail before HTTP egress.
    """
    file = part.get("file")
    if set(part) != {"type", "file"} or not isinstance(file, dict):
        raise ValueError("invalid file input")
    if set(file) != {"filename", "file_data"}:
        raise ValueError("only inline file data is supported")
    name, data = file.get("filename"), file.get("file_data")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("invalid document filename")
    extension = name.rsplit(".", 1)[-1].lower()
    media = {"txt": "text/plain", "pdf": "application/pdf"}.get(extension)
    if media is None or not isinstance(data, str):
        raise ValueError("only text and PDF documents are supported")
    prefix = f"data:{media};base64,"
    if not data.startswith(prefix):
        raise ValueError("document media type does not match its filename")
    encoded = data[len(prefix) :]
    if not encoded or len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4:
        raise ValueError("document exceeds input bounds")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("invalid document encoding") from None
    if (
        not raw
        or len(raw) > MAX_FILE_BYTES
        or base64.b64encode(raw).decode("ascii") != encoded
    ):
        raise ValueError("invalid document encoding or size")
    if media == "application/pdf":
        if not raw.startswith(b"%PDF-"):
            raise ValueError("invalid PDF input")
    else:
        text = raw.decode("utf-8")
        if "\x00" in text:
            raise ValueError("invalid text document")
    return {"type": "input_file", "filename": name, "file_data": data}
