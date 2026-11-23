"""Cross-SDK identities measured from the issued SDK10/native4 application."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from skulk_openai.native_contracts import build_native_manifest, native_bindings
from skulk_openai.settings import IntegrationSettings

pytestmark = pytest.mark.managed
_OBJECT = TypeAdapter(dict[str, JsonValue])
_TEXT = TypeAdapter(str)


def digest(value: JsonValue) -> str:
    """Match the independent baseline's canonical JSON measurement."""
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


@pytest.mark.parametrize("mock", [False, True])
def test_sdk1_preserves_issued_proof_targets_and_state_policy(mock: bool) -> None:
    """A runtime migration must not rename protected caller/checkpoint identities."""
    baseline = _OBJECT.validate_json(
        (Path(__file__).parent / "fixtures/sdk010-native4-identities.json").read_bytes()
    )
    assert baseline["sdk"] == "0.5.10rc1" and baseline["kernel"] == "0.7.0rc4"
    expected = _OBJECT.validate_python(baseline[str(mock)], strict=True)
    observed: list[JsonValue] = [
        {
            "legacy_id": binding.legacy.id,
            "version": binding.legacy.version,
            "legacy_revision": binding.legacy.revision,
            "native_id": _TEXT.validate_python(binding.declaration["id"], strict=True),
            "native_revision": binding.revision,
        }
        for binding in native_bindings(mock=mock)
    ]
    assert observed == expected["bindings"]
    manifest = build_native_manifest(
        executable="bundle.pyz", executable_sha256="a" * 64, mock=mock
    )
    # The candidate gets a new application version; all consent, schema and
    # resource declarations remain bound to their independently measured policy.
    del manifest["bundle_version"]
    assert digest(manifest) == expected["manifest_without_bundle_version_sha256"]
    assert (
        digest(IntegrationSettings().model_dump(mode="json"))
        == baseline["settings_defaults_sha256"]
    )
