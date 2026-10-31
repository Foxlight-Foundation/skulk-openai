"""Capability-owned gateway delegation and replay-safe inference admission."""

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import aclosing

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from skulk_inference_reference.chat import (
    ChatCatalog,
    ChatEvent,
    ChatModel,
    GatewayProof,
)

from .auth_contracts import AuthFailure
from .capability import InferenceCapability
from .chat_request import ChatRequest
from .provider import ProviderFailure
from .settings import IntegrationSettings, SettingsStore, account_binding


class GatewayService:
    """Delegate only to an owner-selected gateway, bounded by current local policy."""

    def __init__(
        self, capability: InferenceCapability, *, callable_id: str, revision: str
    ) -> None:
        """Bind the managed target and exact published chat stream revision."""
        self.capability = capability
        self.callable_id = callable_id
        self.revision = revision
        self.settings = SettingsStore(capability.provider.sessions.store)
        with self.settings.store.transaction() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS gateway_nonces (
                    nonce TEXT PRIMARY KEY, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS gateway_budget (
                    hour INTEGER PRIMARY KEY, attempts INTEGER NOT NULL
                );
            """)

    async def catalog(self) -> ChatCatalog | None:
        """Return cached owner-enabled models; never refresh tokens or upstreams."""
        settings = await asyncio.to_thread(self.settings.read)
        catalog = await self.capability.current_catalog()
        if catalog is None or not (
            settings.inference_enabled
            and settings.processing == "allow_openai"
            and settings.usage == "app_settings"
            and settings.gateway_public_key
            and settings.account_binding
            == account_binding(catalog.registration_id, catalog.generation)
        ):
            return None
        revision = hashlib.sha256(
            (catalog.revision + settings.model_dump_json()).encode()
        ).hexdigest()
        return ChatCatalog(
            target_node=self.capability.target_node,
            callable_id=self.callable_id,
            descriptor_revision=self.revision,
            catalog_revision=revision,
            expires_at=catalog.expires_at,
            models=tuple(
                ChatModel(
                    id=m.slug,
                    name=m.display_name,
                    features=(
                        *(
                            ("image_input",)
                            if settings.allow_images and "image" in m.input_modalities
                            else ()
                        ),
                        *(
                            ("function_tools",)
                            if settings.allow_function_tools
                            and m.supports_parallel_tool_calls
                            else ()
                        ),
                    ),
                )
                for m in catalog.models
                if m.slug in settings.models
            ),
            processing_domain="external",
            usage="unknown",
            usage_label="Using ChatGPT plan",
            usage_url="https://chatgpt.com/settings/usage",
        )

    def _reserve(self, proof: GatewayProof, settings: IntegrationSettings) -> None:
        now = self.capability.clock()
        invocation = proof.invocation
        if not now < invocation.expires_at <= now + 60:
            raise AuthFailure("permission_missing")
        try:
            Ed25519PublicKey.from_public_bytes(
                bytes.fromhex(invocation.public_key)
            ).verify(
                bytes.fromhex(proof.signature),
                invocation.canonical(),
            )
        except (InvalidSignature, ValueError):
            raise AuthFailure("permission_missing") from None
        with self.settings.store.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='integration_settings'"
            ).fetchone()
            if row is None or str(row[0]) != settings.model_dump_json():
                raise AuthFailure("permission_missing")
            # Use the same write serialization as account disconnect so policy
            # and account revocation before this point reject every new call.
            account = connection.execute(
                "SELECT a.registration_id, a.generation, a.enabled, a.tokens "
                "FROM accounts a "
                "JOIN metadata m ON m.key='active' AND m.value=a.registration_id"
            ).fetchone()
            if (
                account is None
                or not account[2]
                or account[3] is None
                or account_binding(str(account[0]), int(account[1]))
                != settings.account_binding
            ):
                raise AuthFailure("permission_missing")
            connection.execute("DELETE FROM gateway_nonces WHERE expires<=?", (now,))
            connection.execute(
                "DELETE FROM gateway_budget WHERE hour<?", (int(now // 3600) - 24,)
            )
            row = connection.execute(
                "SELECT attempts FROM gateway_budget WHERE hour=?", (int(now // 3600),)
            ).fetchone()
            if row is not None and int(row[0]) >= settings.maximum_requests_per_hour:
                raise AuthFailure("permission_missing")
            try:
                connection.execute(
                    "INSERT INTO gateway_nonces VALUES (?, ?)",
                    (invocation.nonce, invocation.expires_at),
                )
            except sqlite3.IntegrityError:
                raise AuthFailure("permission_missing") from None
            connection.execute(
                "INSERT INTO gateway_budget VALUES (?, 1) ON CONFLICT(hour) "
                "DO UPDATE SET attempts=attempts+1",
                (int(now // 3600),),
            )

    async def generate(self, proof: GatewayProof) -> AsyncGenerator[ChatEvent]:
        """Verify scoped owner delegation and signed input before any content egress."""
        try:
            catalog = await self.catalog()
            settings = await asyncio.to_thread(self.settings.read)
            call = proof.invocation
            if catalog is None or (
                call.public_key != settings.gateway_public_key
                or call.target_node != catalog.target_node
                or call.callable_id != self.callable_id
                or call.descriptor_revision != self.revision
                or call.catalog_revision != catalog.catalog_revision
                or call.model not in {m.id for m in catalog.models}
                or not self.capability.clock()
                < call.expires_at
                <= self.capability.clock() + 60
            ):
                raise AuthFailure("permission_missing")
            try:
                request = ChatRequest.model_validate_json(json.dumps(call.request))
                body: dict[str, object] = {
                    **request.responses(settings),
                    "model": call.model,
                }
            except ValueError:
                yield ChatEvent(kind="failed", error="invalid_request")
                return
            upstream_catalog = await self.capability.current_catalog()
            if upstream_catalog is None:
                raise AuthFailure("permission_missing")
            # Refreshing the account catalog changes routing revision; fail
            # rather than admitting against a different snapshot by accident.
            latest = await self.catalog()
            if latest != catalog:
                raise AuthFailure("permission_missing")
            await asyncio.to_thread(self._reserve, proof, settings)
            async with aclosing(
                self.capability.provider.chat(
                    body,
                    catalog=upstream_catalog,
                    maximum_characters=settings.maximum_output_characters,
                )
            ) as events:
                async for event in events:
                    yield event
        except (AuthFailure, ProviderFailure, ValueError, OSError, sqlite3.Error):
            yield ChatEvent(kind="failed", error="policy_denied")
