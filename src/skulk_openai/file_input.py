"""Validate inline document inputs without reading paths or fetching URLs."""

import base64
import binascii
import re

from pydantic import JsonValue

MAX_FILE_BYTES = 8 * 1024 * 1024
_NAME = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,128}$")


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
