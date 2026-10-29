"""Fixed-origin API requests and account-bound picker choices with no provider IO."""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest

from skulk_inference_reference.contracts import (
    GenerationEvent,
    GenerationRequest,
    Message,
)
from skulk_openai.provider import (
    CatalogCache,
    ProviderClient,
    ProviderFailure,
    refresh_cached_catalog,
)
from skulk_openai.responses import event_bytes
from tests.test_sessions import connect, setup


def request() -> GenerationRequest:
    """Build bounded text that a denial must keep away from the HTTP sink."""
    return GenerationRequest(messages=(Message(role="user", content="private input"),))


async def collect(events: AsyncGenerator[GenerationEvent]) -> list[GenerationEvent]:
    """Drain explicit terminal outcomes for account-bound requests."""
    return [event async for event in events]


class API:
    """Synthetic official-origin catalog and finite text Responses stream."""

    def __init__(self) -> None:
        """Retain request observations with synthetic credentials only."""
        self.calls: list[httpx.Request] = []
        self.status = 200
        self.raw: bytes | None = None
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def handle(self, call: httpx.Request) -> httpx.Response:
        """Return current picker metadata or a fixed non-retaining text stream."""
        self.calls.append(call)
        assert call.url.host == "api.openai.com"
        assert call.headers["authorization"].startswith("Bearer synthetic-access")
        if call.url.path == "/v1/models":
            self.entered.set()
            if self.release is not None:
                await self.release.wait()
            return httpx.Response(
                self.status,
                content=self.raw,
                json=None
                if self.raw is not None
                else {
                    "models": [
                        {
                            "slug": "text-b",
                            "display_name": "Second",
                            "visibility": "list",
                        },
                        {
                            "slug": "hidden",
                            "display_name": "Hidden",
                            "visibility": "hide",
                        },
                        {
                            "slug": "text-a",
                            "display_name": "First",
                            "visibility": "list",
                        },
                    ]
                },
            )
        assert call.url.path == "/v1/responses" and call.method == "POST"
        return httpx.Response(
            self.status,
            headers={"content-type": "text/event-stream"},
            content=event_bytes(
                {"type": "response.output_text.delta", "delta": "hello"}
            )
            + event_bytes({"type": "response.completed"}),
        )


async def test_catalog_preserves_display_order_and_only_listed_choices(
    tmp_path: Path,
) -> None:
    manager, issuer, _ = setup(tmp_path)
    await connect(manager, issuer)
    api = API()
    client = ProviderClient(manager, transport=httpx.MockTransport(api.handle))
    catalog = await client.catalog()
    assert [model.slug for model in catalog.models] == ["text-b", "text-a"]
    events = await collect(client.generate(request(), catalog=catalog, model="text-a"))
    assert events[-1].kind == "completed"
    assert len(api.calls) == 2
    assert b'"store":false' in api.calls[-1].content
    assert b'"stream":true' in api.calls[-1].content
    assert b'"model":"text-a"' in api.calls[-1].content
    assert "token" not in catalog.model_dump_json()
    assert "context" not in catalog.model_dump_json()


@pytest.mark.parametrize("denial", ["stale", "hidden", "disconnect", "reconnect"])
async def test_denied_generation_sends_no_prompt(tmp_path: Path, denial: str) -> None:
    manager, issuer, clock = setup(tmp_path)
    await connect(manager, issuer)
    api = API()
    client = ProviderClient(
        manager, transport=httpx.MockTransport(api.handle), clock=clock
    )
    catalog = await client.catalog()
    model = "text-a"
    if denial == "stale":
        clock.now += 61
    elif denial == "hidden":
        model = "hidden"
    elif denial == "disconnect":
        await manager.disconnect()
    else:
        saved = manager.store.load()
        assert saved is not None and saved.tokens is not None
        manager.store.connect(
            saved.subject,
            saved.client_id,
            saved.tokens,
            expected_generation=saved.generation,
            expected_epoch=manager.store.epoch(),
        )
    assert (await collect(client.generate(request(), catalog=catalog, model=model)))[
        -1
    ].error == "policy_denied"
    assert len(api.calls) == 1


async def test_catalog_switch_race_discards_old_choices(tmp_path: Path) -> None:
    manager, issuer, _ = setup(tmp_path)
    await connect(manager, issuer)
    api = API()
    api.release = asyncio.Event()
    client = ProviderClient(manager, transport=httpx.MockTransport(api.handle))
    refresh = asyncio.create_task(client.catalog())
    await asyncio.wait_for(api.entered.wait(), 1)
    saved = manager.store.load()
    assert saved is not None and saved.tokens is not None
    manager.store.connect(
        saved.subject,
        saved.client_id,
        saved.tokens,
        expected_generation=saved.generation,
        expected_epoch=manager.store.epoch(),
    )
    api.release.set()
    with pytest.raises(ProviderFailure, match="stale_catalog"):
        await refresh


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
async def test_http_failure_never_retries_or_follows_redirects(
    tmp_path: Path, status: int
) -> None:
    manager, issuer, _ = setup(tmp_path)
    await connect(manager, issuer)
    api = API()
    client = ProviderClient(manager, transport=httpx.MockTransport(api.handle))
    catalog = await client.catalog()
    api.status = status
    assert (await collect(client.generate(request(), catalog=catalog, model="text-a")))[
        -1
    ].error == "upstream_failed"
    assert len(api.calls) == 2


@pytest.mark.parametrize("raw", [b"invalid", b"x" * 65537, b'{"models":{}}'])
async def test_catalog_is_finite_and_validated(tmp_path: Path, raw: bytes) -> None:
    manager, issuer, _ = setup(tmp_path)
    await connect(manager, issuer)
    api = API()
    api.raw = raw
    client = ProviderClient(manager, transport=httpx.MockTransport(api.handle))
    with pytest.raises(ProviderFailure, match="invalid_catalog"):
        await client.catalog()
    assert len(api.calls) == 1


async def test_owner_metadata_refresh_preserves_cache_on_failure_without_inference(
    tmp_path: Path,
) -> None:
    manager, issuer, clock = setup(tmp_path)
    await connect(manager, issuer)
    api = API()
    client = ProviderClient(
        manager, transport=httpx.MockTransport(api.handle), clock=clock
    )
    assert await refresh_cached_catalog(client)
    original = CatalogCache(manager).read()
    assert original is not None
    api.status = 503
    clock.now += 61
    assert not await refresh_cached_catalog(client)
    assert CatalogCache(manager).read() == original
    assert original.expires_at <= clock()
    assert len(api.calls) == 2 and client.requests_started == 0


async def test_identity_only_metadata_worker_sends_nothing(tmp_path: Path) -> None:
    manager, issuer, _ = setup(tmp_path)
    issuer.scopes = "openid profile email"
    await connect(manager, issuer)
    api = API()
    client = ProviderClient(manager, transport=httpx.MockTransport(api.handle))
    assert not await refresh_cached_catalog(client)
    assert api.calls == [] and CatalogCache(manager).read() is None
