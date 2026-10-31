"""The managed capability's independent admission boundary, using public contracts."""

import asyncio
import sqlite3
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing

from pydantic import Field

from skulk_inference_reference.contracts import (
    FrozenContract,
    GenerationEvent,
    Identifier,
    Offer,
)

from .admission import GrantStore, SignedInvocation, catalog_offer
from .auth_contracts import AuthFailure
from .provider import CatalogCache, CatalogSnapshot, ProviderClient, ProviderFailure
from .settings import SettingsStore


class InferenceDiscovery(FrozenContract):
    """Bounded cached choices and the exact revision needed for a signed call."""

    target_node: Identifier
    catalog_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    offers: tuple[Offer, ...] = Field(default=(), max_length=16)


class InferenceCapability:
    """Own cached discovery and direct-call enforcement before upstream content IO."""

    def __init__(
        self,
        provider: ProviderClient,
        *,
        target_node: str,
        callable_id: str,
        descriptor_revision: str,
        enabled: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind the actual managed target, local account store and immutable schema."""
        self.provider = provider
        self.target_node = target_node
        self.callable_id = callable_id
        self.descriptor_revision = descriptor_revision
        self.settings = SettingsStore(provider.sessions.store)
        self._startup_enabled = enabled
        self.clock = clock
        self.grants = GrantStore(provider.sessions.store, clock=clock)
        self.cache = CatalogCache(provider.sessions)

    @property
    def enabled(self) -> bool:
        """Use current saved owner policy, or the legacy startup default."""
        return (
            self.settings.read().inference_enabled
            if self.settings.configured()
            else self._startup_enabled
        )

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Update the legacy default without overriding a persisted owner choice."""
        self._startup_enabled = value

    async def current_catalog(self) -> CatalogSnapshot | None:
        """Read a fresh account-bound catalog without contacting the provider."""
        if not self.enabled:
            return None
        catalog = await asyncio.to_thread(self.cache.read)
        status = await self.provider.sessions.status()
        if (
            catalog is None
            or catalog.expires_at <= self.clock()
            or status.state != "authorized"
            or status.registration_id != catalog.registration_id
            or status.generation != catalog.generation
        ):
            return None
        return catalog

    async def discovery(self) -> InferenceDiscovery:
        """Publish at most sixteen cached choices without upstream HTTP or refresh."""
        try:
            catalog = await self.current_catalog()
            if catalog is None:
                return InferenceDiscovery(target_node=self.target_node)
            settings = self.settings.read() if self.settings.configured() else None
            if settings is not None and (
                settings.processing != "allow_openai"
                or settings.usage != "app_settings"
            ):
                return InferenceDiscovery(target_node=self.target_node)
            offers = tuple(
                catalog_offer(
                    catalog,
                    model.slug,
                    callable_id=self.callable_id,
                    descriptor_revision=self.descriptor_revision,
                )
                for model in catalog.models[:16]
                if settings is None or model.slug in settings.models
            )
            return InferenceDiscovery(
                target_node=self.target_node,
                catalog_revision=catalog.revision,
                offers=offers,
            )
        except (AuthFailure, ProviderFailure, OSError, ValueError, sqlite3.Error):
            return InferenceDiscovery(target_node=self.target_node)

    async def offers(self) -> tuple[Offer, ...]:
        """Return cached offers; use discovery for target/catalog revision binding."""
        return (await self.discovery()).offers

    async def generate(
        self, signed: SignedInvocation
    ) -> AsyncGenerator[GenerationEvent]:
        """Enforce every direct invocation, independently of caller-side routing."""
        try:
            catalog = await self.current_catalog()
            if catalog is None:
                raise AuthFailure("permission_missing")
            await asyncio.to_thread(
                self.grants.admit,
                signed,
                catalog=catalog,
                target_node=self.target_node,
                callable_id=self.callable_id,
                descriptor_revision=self.descriptor_revision,
            )
            async with aclosing(
                self.provider.generate(
                    signed.invocation.request,
                    catalog=catalog,
                    model=signed.invocation.model,
                )
            ) as events:
                async for event in events:
                    yield event
        except (AuthFailure, ProviderFailure, OSError, ValueError, sqlite3.Error):
            yield GenerationEvent(kind="failed", error="policy_denied")
