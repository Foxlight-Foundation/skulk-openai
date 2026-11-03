"""Capability-owned gateway delegation and replay-safe inference admission."""

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import aclosing

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from skulk_inference_reference.chat import (
    ChatCatalog,
    ChatEvent,
    ChatModel,
    GatewayProof,
)
from skulk_inference_reference.input_body import (
    StreamedCatalog,
    StreamedChatModel,
    StreamedProof,
    receive_body,
)

from .auth_contracts import AuthFailure
from .capability import InferenceCapability
from .chat_request import ChatRequest
from .file_input import document_features, validate_document_features
from .provider import ProviderFailure
from .settings import IntegrationSettings, SettingsStore, account_binding


class GatewayService:
    """Delegate only to an owner-selected gateway, bounded by current local policy."""

    def __init__(
        self,
        capability: InferenceCapability,
        *,
        callable_id: str,
        revision: str,
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

    async def streamed_catalog(self) -> StreamedCatalog | None:
        """Project identical owner authority into the versioned media contract."""
        catalog = await self.catalog()
        if catalog is None:
            return None
        upstream = await self.capability.current_catalog()
        settings = await asyncio.to_thread(self.settings.read)
        if (
            upstream is None
            or hashlib.sha256(
                (
                    upstream.model_dump_json(exclude={"expires_at"})
                    + settings.model_dump_json()
                ).encode()
            ).hexdigest()
            != catalog.catalog_revision
        ):
            return None
        models = {model.slug: model for model in upstream.models}
        return StreamedCatalog.model_validate(
            {
                **catalog.model_dump(
                    exclude={"contract", "callable_version", "models"}
                ),
                "contract": "chat.completions@0.2.0",
                "callable_version": "0.2.0",
                "models": tuple(
                    StreamedChatModel(
                        id=model.id,
                        name=model.name,
                        features=(
                            *model.features,
                            *document_features(
                                models[model.id].input_modalities,
                                allow_images=settings.allow_images,
                            ),
                        ),
                    ).model_dump()
                    for model in catalog.models
                ),
            }
        )

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
        # Renewing identical picker facts is not a revocation. Freshness is
        # checked separately on every read and again before provider submission;
        # account generation, model claims and all owner policy remain bound.
        revision = hashlib.sha256(
            (
                catalog.model_dump_json(exclude={"expires_at"})
                + settings.model_dump_json()
            ).encode()
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

    def _reserve(
        self, proof: GatewayProof | StreamedProof, settings: IntegrationSettings
    ) -> None:
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
            if (
                settings.maximum_requests_per_hour is not None
                and row is not None
                and int(row[0]) >= settings.maximum_requests_per_hour
            ):
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
        async with aclosing(self._generate(proof)) as events:
            async for event in events:
                yield event

    async def generate_streamed(
        self, proof: StreamedProof, chunks: AsyncIterable[bytes]
    ) -> AsyncGenerator[ChatEvent]:
        """Accept only complete, signed media bodies under current owner policy."""
        async with aclosing(self._generate(proof, chunks=chunks)) as events:
            async for event in events:
                yield event

    async def _generate(
        self,
        proof: GatewayProof | StreamedProof,
        *,
        chunks: AsyncIterable[bytes] | None = None,
    ) -> AsyncGenerator[ChatEvent]:
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
            # Authenticate the small reference before accepting document bytes.
            # Admission is still reserved only after the complete body verifies.
            try:
                Ed25519PublicKey.from_public_bytes(
                    bytes.fromhex(call.public_key)
                ).verify(bytes.fromhex(proof.signature), call.canonical())
            except (InvalidSignature, ValueError):
                raise AuthFailure("permission_missing") from None
            try:
                if isinstance(proof, StreamedProof):
                    if chunks is None:
                        raise ValueError("streamed request requires media")
                    payload = await receive_body(proof.invocation.body, chunks)
                else:
                    payload = proof.invocation.request
                request = ChatRequest.model_validate_json(json.dumps(payload))
                translated = request.responses(settings)
                body: dict[str, object] = {
                    **translated,
                    "model": call.model,
                }
            except ValueError:
                yield ChatEvent(kind="failed", error="invalid_request")
                return
            upstream_catalog = await self.capability.current_catalog()
            if upstream_catalog is None:
                raise AuthFailure("permission_missing")
            upstream_model = next(
                (
                    model
                    for model in upstream_catalog.models
                    if model.slug == call.model
                ),
                None,
            )
            if upstream_model is None:
                raise AuthFailure("permission_missing")
            try:
                validate_document_features(
                    translated,
                    document_features(
                        upstream_model.input_modalities,
                        allow_images=settings.allow_images,
                    ),
                )
            except ValueError:
                yield ChatEvent(kind="failed", error="invalid_request")
                return
            # A freshness-only renewal may occur between these reads. All
            # authority and model claims must still match the signed snapshot.
            latest = await self.catalog()
            if latest is None or latest.model_dump(
                exclude={"expires_at"}
            ) != catalog.model_dump(exclude={"expires_at"}):
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
