"""Capability-owned connection lifecycle with no Skulk core integration."""

import asyncio
import time
from collections.abc import Callable

from pydantic import SecretStr

from .auth_contracts import (
    PLAN_SCOPE,
    RESOURCE,
    AuthFailure,
    ConnectionStatus,
    Registration,
    TokenResponse,
    TokenSet,
)
from .credentials import CredentialStore
from .identity import verify_identity
from .oauth import AuthorizationAttempt, OAuthClient


class SessionManager:
    """Own account consent, protected storage, serialized refresh and disconnect."""

    def __init__(
        self,
        store: CredentialStore,
        client: OAuthClient,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Bind the provider's owner-local storage and fixed-issuer client."""
        self.store = store
        self.client = client
        self.clock = clock

    async def status(self) -> ConnectionStatus:
        """Read local facts only; health/status never performs upstream HTTP."""
        registration = await asyncio.to_thread(self.store.load)
        if (
            registration is None
            or not registration.enabled
            or registration.tokens is None
        ):
            return ConnectionStatus(state="disconnected")
        tokens = registration.tokens
        state = (
            "expired"
            if tokens.expires_at <= self.clock()
            else "authorized"
            if PLAN_SCOPE in tokens.scopes
            else "identity_only"
        )
        return ConnectionStatus(
            state=state,
            registration_id=registration.registration_id,
            generation=registration.generation,
        )

    async def begin(
        self,
        redirect_uri: str,
        *,
        registration_id: str | None = None,
        request_plan_consent: bool = False,
    ) -> AuthorizationAttempt:
        """Create owner-local browser state, keeping the existing connection intact."""
        selected = None
        if registration_id is not None:
            selected = await asyncio.to_thread(self.store.load, registration_id)
            if selected is None:
                raise AuthFailure("sign_in_required")
        return self.client.begin(
            host_id=await asyncio.to_thread(self.store.host_id),
            redirect_uri=redirect_uri,
            epoch=await asyncio.to_thread(self.store.epoch),
            selected=selected,
            request_plan_consent=request_plan_consent,
        )

    def _tokens(
        self, response: TokenResponse, previous: TokenSet | None = None
    ) -> TokenSet:
        now = self.clock()
        if response.token_type.lower() != "bearer":
            raise AuthFailure("invalid_response")
        scopes = (
            tuple(sorted(set(response.scope.split())))
            if response.scope is not None
            else previous.scopes
            if previous is not None
            else ()
        )
        if len(scopes) > 32 or any(len(scope) > 128 for scope in scopes):
            raise AuthFailure("invalid_response")
        id_token = response.id_token or (previous.id_token if previous else None)
        if id_token is None:
            raise AuthFailure("invalid_response")
        if "offline_access" in scopes and response.refresh_token is None:
            raise AuthFailure("invalid_response")
        expires = now + response.expires_in
        refresh_at = min(
            expires, max(now, response.earliest_refresh_at or expires - 60)
        )
        return TokenSet(
            access_token=response.access_token,
            refresh_token=response.refresh_token,
            id_token=id_token,
            scopes=scopes,
            expires_at=expires,
            refresh_at=refresh_at,
        )

    async def complete(
        self, attempt: AuthorizationAttempt, query: str
    ) -> ConnectionStatus:
        """Validate and persist a single OAuth callback without granting fabric use."""
        code, client_id = self.client.consume(attempt, query)
        response, keys = await self.client.exchange(attempt, code, client_id)
        if response.id_token is None:
            raise AuthFailure("identity_invalid")
        identity = verify_identity(
            response.id_token.get_secret_value(),
            keys,
            client_id=client_id,
            nonce=attempt.nonce.get_secret_value(),
            subject=attempt.selected.subject if attempt.selected else None,
        )
        tokens = self._tokens(response)
        try:
            await asyncio.to_thread(
                self.store.connect,
                identity.sub,
                client_id,
                tokens,
                expected_generation=attempt.selected.generation
                if attempt.selected
                else None,
                expected_epoch=attempt.epoch,
            )
        except AuthFailure:
            await self._revoke(client_id, tokens)
            raise
        return await self.status()

    async def _revoke(self, client_id: str, tokens: TokenSet) -> bool:
        if tokens.refresh_token is None:
            return True
        try:
            metadata = await self.client.discovery()
            await self.client.request(
                "POST",
                metadata.revocation_endpoint,
                empty_ok=True,
                data={
                    "token": tokens.refresh_token.get_secret_value(),
                    "token_type_hint": "refresh_token",
                    "client_id": client_id,
                },
            )
            return True
        except AuthFailure:
            return False

    async def disconnect(self, registration_id: str | None = None) -> bool:
        """Fence admission and clear local tokens before best-effort remote revocation.

        The returned boolean only confirms the remote revocation request, not
        interruption or refund of an already running provider operation.
        """
        previous = await asyncio.to_thread(self.store.disconnect, registration_id)
        if previous is None or previous.tokens is None:
            return True
        return await self._revoke(previous.client_id, previous.tokens)

    async def access_token(self, registration_id: str, generation: int) -> SecretStr:
        """Admit only the selected, enabled account/revision with granted plan usage."""
        async with self.store.session_lock(registration_id):
            current = await asyncio.to_thread(self.store.load)
            if (
                current is None
                or current.registration_id != registration_id
                or current.generation != generation
                or not current.enabled
                or current.tokens is None
            ):
                raise AuthFailure("sign_in_required")
            if PLAN_SCOPE not in current.tokens.scopes:
                raise AuthFailure("permission_missing")
            if current.tokens.refresh_at <= self.clock():
                current = await self._refresh(current)
            # Disconnect can fence a network refresh or a second process's read.
            latest = await asyncio.to_thread(self.store.load)
            if (
                latest is None
                or latest.registration_id != registration_id
                or latest.generation != generation
                or not latest.enabled
                or latest.tokens is None
            ):
                raise AuthFailure("sign_in_required")
            if PLAN_SCOPE not in latest.tokens.scopes:
                raise AuthFailure("permission_missing")
            if latest.tokens.expires_at <= self.clock():
                raise AuthFailure("sign_in_required")
            return latest.tokens.access_token

    async def _refresh(self, current: Registration) -> Registration:
        previous = current.tokens
        if previous is None or previous.refresh_token is None:
            raise AuthFailure("sign_in_required")
        metadata = await self.client.discovery()
        # Fetch verification material before any rotating token can be consumed.
        keys = await self.client.request("GET", metadata.jwks_uri)
        try:
            payload = await self.client.request(
                "POST",
                metadata.token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "client_id": current.client_id,
                    "refresh_token": previous.refresh_token.get_secret_value(),
                    "resource": RESOURCE,
                },
            )
        except AuthFailure as error:
            if error.code == "session_revoked":
                await asyncio.to_thread(
                    self.store.disconnect,
                    current.registration_id,
                    expected_generation=current.generation,
                )
            raise
        try:
            response = TokenResponse.model_validate(payload)
        except ValueError:
            raise AuthFailure("invalid_response") from None
        if response.id_token is not None:
            verify_identity(
                response.id_token.get_secret_value(),
                keys,
                client_id=current.client_id,
                nonce=None,
                subject=current.subject,
            )
        tokens = self._tokens(response, previous)
        commit = asyncio.create_task(
            asyncio.to_thread(self.store.replace_tokens, current, tokens)
        )
        try:
            replaced = await asyncio.shield(commit)
        except asyncio.CancelledError:
            await commit
            raise
        if not replaced:
            await self._revoke(current.client_id, tokens)
            raise AuthFailure("stale_authorization")
        result = await asyncio.to_thread(self.store.load, current.registration_id)
        if result is None:
            raise AuthFailure("sign_in_required")
        return result
