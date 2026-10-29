"""Account-bound official API client; fabric callers need separate admission."""

import asyncio
import hashlib
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from typing import Literal, final

import httpx
from pydantic import ConfigDict, Field

from skulk_inference_reference.contracts import (
    FrozenContract,
    GenerationEvent,
    GenerationRequest,
    Identifier,
)

from .auth_contracts import AuthFailure, Registration
from .responses import response_events
from .sessions import SessionManager


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
                            if len(raw) + len(chunk) > 65536:
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
