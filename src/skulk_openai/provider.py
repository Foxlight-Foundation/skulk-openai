"""Account-bound official API client; fabric callers need separate admission."""

import asyncio
import hashlib
import sqlite3
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from typing import Literal, final

import httpx
from pydantic import ConfigDict, Field

from skulk_inference_reference.chat import ChatEvent
from skulk_inference_reference.contracts import (
    FrozenContract,
    GenerationEvent,
    GenerationRequest,
    Identifier,
)

from .auth_contracts import AuthFailure, Registration
from .chat_stream import chat_events
from .responses import response_events
from .sessions import SessionManager

# The upstream catalog includes large model instructions that we discard. Its
# download ceiling must be separate from the compact persisted snapshot limit.
_MAX_CATALOG_RESPONSE_BYTES = 1024 * 1024


class ProviderFailure(Exception):
    """A content-free catalog outcome, never an upstream response body."""

    def __init__(
        self,
        code: Literal["catalog_unavailable", "invalid_catalog", "stale_catalog"],
    ) -> None:
        """Retain only an actionable fixed failure category."""
        super().__init__(code)
        self.code = code


@final
class CatalogModel(FrozenContract):
    """Provider-supplied picker metadata; listing does not establish entitlement."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")
    slug: Identifier
    display_name: str = Field(min_length=1, max_length=256)
    visibility: str = Field(max_length=32)
    input_modalities: tuple[Identifier, ...] = Field(default=(), max_length=16)
    supports_parallel_tool_calls: bool = False


@final
class CatalogResponse(FrozenContract):
    """Bounded account-specific catalog without inferred feature or usage claims."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")
    models: tuple[CatalogModel, ...] = Field(max_length=128)


@final
class CatalogSnapshot(FrozenContract):
    """Finite model choices bound to the exact selected account generation."""

    registration_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    generation: int = Field(ge=1)
    models: tuple[CatalogModel, ...] = Field(max_length=128)
    expires_at: float = Field(gt=0)

    @property
    def revision(self) -> str:
        """Bind selection to every immutable field, including freshness."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class ProviderClient:
    """Use only the official API with capability-owned credentials and no retries.

    This is an owner-local API primitive, not an authenticated fabric handler.
    A managed capability must enforce caller authority before entering it.
    """

    def __init__(
        self,
        sessions: SessionManager,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_live: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Require an explicit mock transport or deliberate live-mode construction."""
        if transport is None and not allow_live:
            raise ValueError("live provider access requires explicit owner intent")
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise ValueError("tests require an explicit mock transport")
        self.sessions = sessions
        self.transport = transport
        self.clock = clock
        self.requests_started = 0

    async def _current(self) -> Registration:
        current = await asyncio.to_thread(self.sessions.store.load)
        if current is None or not current.enabled or current.tokens is None:
            raise AuthFailure("sign_in_required")
        return current

    async def catalog(self) -> CatalogSnapshot:
        """Explicitly refresh picker choices; health/offers must use cached facts."""
        account = await self._current()
        token = await self.sessions.access_token(
            account.registration_id, account.generation
        )
        try:
            async with asyncio.timeout(10):
                async with httpx.AsyncClient(
                    transport=self.transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=5,
                ) as client:
                    async with client.stream(
                        "GET",
                        "https://api.openai.com/v1/models",
                        headers={"Authorization": "Bearer " + token.get_secret_value()},
                    ) as response:
                        if response.status_code != 200:
                            raise ProviderFailure("catalog_unavailable")
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(raw) + len(chunk) > _MAX_CATALOG_RESPONSE_BYTES:
                                raise ProviderFailure("invalid_catalog")
                            raw.extend(chunk)
            parsed = CatalogResponse.model_validate_json(raw)
        except (httpx.HTTPError, TimeoutError):
            raise ProviderFailure("catalog_unavailable") from None
        except ValueError:
            raise ProviderFailure("invalid_catalog") from None
        if len({model.slug for model in parsed.models}) != len(parsed.models):
            raise ProviderFailure("invalid_catalog")
        latest = await self._current()
        if (
            latest.registration_id != account.registration_id
            or latest.generation != account.generation
        ):
            raise ProviderFailure("stale_catalog")
        return CatalogSnapshot(
            registration_id=account.registration_id,
            generation=account.generation,
            models=tuple(
                model for model in parsed.models if model.visibility == "list"
            ),
            expires_at=self.clock() + 60,
        )

    async def generate(
        self,
        request: GenerationRequest,
        *,
        catalog: CatalogSnapshot,
        model: str,
    ) -> AsyncGenerator[GenerationEvent]:
        """Stream one selected model after account/freshness rechecks; never replay.

        Callers must enforce their own trusted admission before this primitive.
        Expired choices and account changes fail before the API sees content.
        """
        try:
            if catalog.expires_at <= self.clock() or model not in {
                item.slug for item in catalog.models
            }:
                yield GenerationEvent(kind="failed", error="policy_denied")
                return
            token = await self.sessions.access_token(
                catalog.registration_id, catalog.generation
            )
            # Refresh can take time; recheck catalog freshness before submission.
            if catalog.expires_at <= self.clock():
                yield GenerationEvent(kind="failed", error="policy_denied")
                return
            async with httpx.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=5,
            ) as client:
                self.requests_started = min(self.requests_started + 1, 2**63 - 1)
                async with client.stream(
                    "POST",
                    "https://api.openai.com/v1/responses",
                    headers={"Authorization": "Bearer " + token.get_secret_value()},
                    json={
                        "model": model,
                        "store": False,
                        "stream": True,
                        "input": [message.model_dump() for message in request.messages],
                    },
                ) as response:
                    async with aclosing(response_events(response, request)) as events:
                        async for event in events:
                            yield event
        except AuthFailure:
            yield GenerationEvent(kind="failed", error="policy_denied")
        except (httpx.HTTPError, TimeoutError):
            yield GenerationEvent(kind="failed", error="upstream_failed")

    async def chat(
        self,
        body: dict[str, object],
        *,
        catalog: CatalogSnapshot,
        maximum_characters: int,
    ) -> AsyncGenerator[ChatEvent]:
        """Submit one pre-admitted chat request, without replay or stored context."""
        try:
            token = await self.sessions.access_token(
                catalog.registration_id, catalog.generation
            )
            if catalog.expires_at <= self.clock():
                yield ChatEvent(kind="failed", error="policy_denied")
                return
            async with httpx.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(120, connect=5),
            ) as client:
                self.requests_started = min(self.requests_started + 1, 2**63 - 1)
                async with client.stream(
                    "POST",
                    "https://api.openai.com/v1/responses",
                    headers={"Authorization": "Bearer " + token.get_secret_value()},
                    json=body,
                ) as response:
                    async with aclosing(
                        chat_events(response, maximum_characters=maximum_characters)
                    ) as events:
                        async for event in events:
                            yield event
        except AuthFailure:
            yield ChatEvent(kind="failed", error="policy_denied")
        except (httpx.HTTPError, TimeoutError):
            yield ChatEvent(kind="failed", error="upstream_failed")


class CatalogCache:
    """Persist non-secret picker facts locally; reading never requests a token."""

    def __init__(self, sessions: SessionManager) -> None:
        """Use this capability's protected session database."""
        self.store = sessions.store

    def save(self, snapshot: CatalogSnapshot) -> None:
        """Save only while the selected account/generation still matches."""
        encoded = snapshot.model_dump_json()
        if len(encoded.encode()) > 65536:
            raise ProviderFailure("invalid_catalog")
        with self.store.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT a.registration_id, a.generation, a.enabled FROM accounts a "
                "JOIN metadata m ON m.key='active' AND m.value=a.registration_id"
            ).fetchone()
            if (
                row is None
                or str(row[0]) != snapshot.registration_id
                or int(row[1]) != snapshot.generation
                or not row[2]
            ):
                raise ProviderFailure("stale_catalog")
            connection.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('catalog', ?)", (encoded,)
            )

    def read(self) -> CatalogSnapshot | None:
        """Read cached facts; the serving boundary independently checks freshness."""
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='catalog'"
            ).fetchone()
        if row is None:
            return None
        raw = str(row[0])
        if len(raw.encode()) > 65536:
            raise ProviderFailure("invalid_catalog")
        try:
            return CatalogSnapshot.model_validate_json(raw)
        except ValueError:
            raise ProviderFailure("invalid_catalog") from None


async def refresh_cached_catalog(provider: ProviderClient) -> bool:
    """Refresh metadata once, preserving bounded cached facts on failure.

    This performs no inference. Call only from an explicitly enabled owner's
    metadata worker; health/discovery themselves must never invoke it.
    """
    try:
        status = await provider.sessions.status()
        if status.state not in {"authorized", "expired"}:
            return False
        snapshot = await provider.catalog()
        await asyncio.to_thread(CatalogCache(provider.sessions).save, snapshot)
        return True
    except (AuthFailure, ProviderFailure, OSError, ValueError, sqlite3.Error):
        return False
