"""Owner caps are optional without widening explicit grants or bypassing policy."""

import secrets
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from skulk_openai.chat_stream import chat_events
from skulk_openai.management import ManagementServer
from skulk_openai.responses import event_bytes
from skulk_openai.settings import IntegrationSettings, SettingsStore
from tests.test_admission import invocation
from tests.test_gateway_chat import configured, resign
from tests.test_management import browser


def test_default_policy_has_no_owner_caps_and_allows_images() -> None:
    """New settings have no invented plan allowance or response-length limit."""
    settings = IntegrationSettings()
    assert settings.maximum_requests_per_hour is None
    assert settings.maximum_output_characters is None
    assert settings.allow_images
    assert not settings.inference_enabled
    assert settings.processing == "local_only"


@pytest.mark.parametrize(
    "field", ["maximum_requests_per_hour", "maximum_output_characters"]
)
@pytest.mark.parametrize("invalid", [0, -1, True, 1.5, "60", 9007199254740992])
def test_opted_in_caps_require_a_positive_exact_browser_integer(
    field: str, invalid: object
) -> None:
    """Null means disabled; zero, coercion and rounded browser integers do not."""
    with pytest.raises(ValidationError):
        IntegrationSettings.model_validate({field: invalid})


async def test_saved_caps_survive_upgrade_and_can_be_removed_over_http(
    tmp_path: Path,
) -> None:
    """Keep existing owner choices until the owner clears their toggles."""
    capability, _, settings, _, _, _, _ = await configured(tmp_path)
    saved = settings.model_copy(update={"maximum_output_characters": 32768})
    capability.settings.save(saved)
    assert SettingsStore(capability.provider.sessions.store).read() == saved
    async with browser(ManagementServer(capability)) as client:
        body = saved.model_dump(mode="json", exclude={"account_binding"})
        body.update(
            maximum_requests_per_hour=None,
            maximum_output_characters=None,
            allow_images=True,
        )
        response = await client.post("/api/settings", json=body)
        assert response.status_code == 200
        state = (await client.get("/api/state")).json()
        assert state["settings"]["maximum_requests_per_hour"] is None
        assert state["settings"]["maximum_output_characters"] is None
        assert state["settings"]["allow_images"] is True
    restored = SettingsStore(capability.provider.sessions.store).read()
    assert restored.maximum_requests_per_hour is None
    assert restored.maximum_output_characters is None
    assert restored.account_binding == saved.account_binding
    assert capability.provider.requests_started == 0


async def test_uncapped_calls_pass_sixty_and_still_enforce_finite_grants(
    tmp_path: Path,
) -> None:
    """A shared hour can exceed 60 through chat and legacy without widening grants."""
    capability, service, settings, key, proof, _, clock = await configured(tmp_path)
    service.settings.save(
        settings.model_copy(update={"maximum_requests_per_hour": None})
    )
    catalog = await service.catalog()
    assert catalog is not None
    proof = resign(proof, key, catalog_revision=catalog.catalog_revision)
    upstream_catalog = capability.cache.read()
    assert upstream_catalog is not None
    credential = capability.grants.issue(
        upstream_catalog,
        models=("text-a",),
        maximum_attempts=2,
        maximum_output_characters=1024,
        lifetime_seconds=300,
        allow_external_unknown_usage=True,
    )
    for index in range(65):
        if index in {1, 32}:
            events = [
                e
                async for e in capability.generate(
                    credential.sign(invocation(upstream_catalog, clock))
                )
            ]
        else:
            events = [
                e
                async for e in service.generate(
                    resign(proof, key, nonce=secrets.token_hex(32))
                )
            ]
        assert events[-1].kind == "completed"
    assert capability.provider.requests_started == 65
    events = [
        e
        async for e in capability.generate(
            credential.sign(invocation(upstream_catalog, clock))
        )
    ]
    assert events[-1].error == "policy_denied"
    assert capability.provider.requests_started == 65
    # Opting in mid-hour includes already admitted attempts, across both surfaces.
    service.settings.save(settings.model_copy(update={"maximum_requests_per_hour": 65}))
    catalog = await service.catalog()
    assert catalog is not None
    changed = resign(
        proof,
        key,
        catalog_revision=catalog.catalog_revision,
        nonce=secrets.token_hex(32),
    )
    assert [e.error async for e in service.generate(changed)] == ["policy_denied"]
    assert capability.provider.requests_started == 65


@pytest.mark.parametrize("maximum", [None, 32768])
@pytest.mark.parametrize(
    "kind", ["response.output_text.delta", "response.reasoning_summary_text.delta"]
)
async def test_long_output_completes_only_without_an_owner_cap(
    maximum: int | None, kind: str
) -> None:
    """Stream beyond both former bounds; an explicit cap still stops before egress."""
    raw = b"".join(event_bytes({"type": kind, "delta": "x" * 4096}) for _ in range(40))
    raw += event_bytes({"type": "response.completed"})
    events = [
        e
        async for e in chat_events(
            httpx.Response(200, content=raw), maximum_characters=maximum
        )
    ]
    if maximum is None:
        assert events[-1].kind == "completed"
        assert (
            sum(len(str(value)) for e in events for value in e.delta.values()) == 163840
        )
    else:
        assert events[-1].error == "output_limit"
        assert (
            sum(len(str(value)) for e in events for value in e.delta.values())
            == maximum
        )
