"""Provider-owned OAuth client: fixed issuer, bounded HTTP and one-use PKCE state."""

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from pydantic import ConfigDict, JsonValue, SecretStr, TypeAdapter

from skulk_inference_reference.contracts import FrozenContract

from .auth_contracts import ISSUER, RESOURCE, AuthFailure, Registration, TokenResponse

_OBJECT = TypeAdapter(dict[str, JsonValue])
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
TERMINAL_REFRESH_ERRORS = frozenset(
    {
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    }
)


class Discovery(FrozenContract):
    """Issuer metadata constrained to documented origins and endpoints."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    revocation_endpoint: str

    def validate_endpoints(self) -> None:
        """Prevent discovery from redirecting tokens or identities to another origin."""
        expected = (
            (self.issuer, ISSUER),
            (self.authorization_endpoint, ISSUER + "/api/accounts/authorize"),
            (self.token_endpoint, ISSUER + "/api/accounts/oauth/token"),
            (self.jwks_uri, ISSUER + "/.well-known/jwks.json"),
        )
        if any(actual != required for actual, required in expected):
            raise AuthFailure("invalid_response")
        endpoint = urlsplit(self.revocation_endpoint)
        if (
            endpoint.scheme != "https"
            or endpoint.netloc != "auth.openai.com"
            or not endpoint.path.startswith("/api/accounts/")
            or endpoint.query
            or endpoint.fragment
        ):
            raise AuthFailure("invalid_response")


@dataclass(frozen=True)
class AuthorizationAttempt:
    """One short-lived owner-local transaction; never expose its hints in logs."""

    state: SecretStr = field(repr=False)
    nonce: SecretStr = field(repr=False)
    verifier: SecretStr = field(repr=False)
    redirect_uri: str
    url: SecretStr = field(repr=False)
    expires_at: float
    epoch: int
    selected: Registration | None = field(default=None, repr=False)


class OAuthClient:
    """A capability-owned issuer client; tests supply a synthetic HTTP transport."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_live: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Require explicit owner intent before enabling real authentication HTTP."""
        if transport is None and not allow_live:
            raise ValueError("live authentication requires explicit owner setup")
        if transport is not None and not isinstance(transport, httpx.MockTransport):
            raise ValueError("tests require an explicit mock transport")
        self.transport = transport
        self.clock = clock
        self._pending: dict[str, AuthorizationAttempt] = {}

    async def request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        empty_ok: bool = False,
    ) -> dict[str, JsonValue]:
        """Read finite issuer JSON; refuse redirects and sanitize every failure."""
        endpoint = urlsplit(url)
        if (
            endpoint.scheme != "https"
            or endpoint.netloc != "auth.openai.com"
            or endpoint.query
            or endpoint.fragment
        ):
            raise AuthFailure("invalid_response")
        try:
            async with asyncio.timeout(10):
                async with httpx.AsyncClient(
                    transport=self.transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=8,
                ) as client:
                    async with client.stream(method, url, data=data) as response:
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(raw) + len(chunk) > 65536:
                                raise AuthFailure("invalid_response")
                            raw.extend(chunk)
                        if response.status_code >= 500:
                            raise AuthFailure("provider_unavailable")
                        if response.status_code != 200:
                            if 400 <= response.status_code < 500:
                                try:
                                    value = _OBJECT.validate_json(raw)
                                except ValueError:
                                    value = {}
                                code = value.get("error")
                                if isinstance(code, dict):
                                    code = code.get("code")
                                terminal = (
                                    TERMINAL_REFRESH_ERRORS
                                    if data is not None
                                    and data.get("grant_type") == "refresh_token"
                                    else frozenset({"invalid_grant"})
                                )
                                if isinstance(code, str) and code in terminal:
                                    raise AuthFailure("session_revoked")
                            raise AuthFailure("invalid_response")
                        if not raw and empty_ok:
                            return {}
                        return _OBJECT.validate_json(raw)
        except (httpx.HTTPError, TimeoutError):
            raise AuthFailure("provider_unavailable") from None
        except ValueError:
            raise AuthFailure("invalid_response") from None

    async def discovery(self) -> Discovery:
        """Fetch and validate trusted endpoint metadata without following redirects."""
        value = await self.request("GET", ISSUER + "/.well-known/openid-configuration")
        try:
            metadata = Discovery.model_validate(value)
            metadata.validate_endpoints()
            return metadata
        except ValueError:
            raise AuthFailure("invalid_response") from None

    def begin(
        self,
        *,
        host_id: str,
        redirect_uri: str,
        epoch: int,
        selected: Registration | None = None,
        request_plan_consent: bool = False,
    ) -> AuthorizationAttempt:
        """Create a ten-minute PKCE transaction with an exact loopback callback."""
        try:
            target = urlsplit(redirect_uri)
            port = target.port
        except ValueError:
            raise AuthFailure("invalid_callback") from None
        if (
            target.scheme != "http"
            or target.hostname != "127.0.0.1"
            or target.path != "/auth/callback"
            or target.query
            or target.fragment
            or target.username is not None
            or target.password is not None
            or port is None
            or port < 1
            or target.netloc != f"127.0.0.1:{port}"
        ):
            raise AuthFailure("invalid_callback")
        self._pending = {
            key: value
            for key, value in self._pending.items()
            if value.expires_at > self.clock()
        }
        if len(self._pending) >= 4:
            raise AuthFailure("invalid_callback")
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        parameters = {
            "client_id": selected.client_id if selected else "dynamic_agent_client",
            "ext_agent_host_id": host_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if selected is None:
            parameters["agent_name_hint"] = "Skulk OpenAI Capability"
        elif selected.tokens is not None:
            parameters["id_token_hint"] = selected.tokens.id_token.get_secret_value()
        if request_plan_consent:
            # Never force consent on ordinary reconnection; this is owner intent.
            parameters["prompt"] = "consent"
        attempt = AuthorizationAttempt(
            state=SecretStr(state),
            nonce=SecretStr(nonce),
            verifier=SecretStr(verifier),
            redirect_uri=redirect_uri,
            url=SecretStr(ISSUER + "/api/accounts/authorize?" + urlencode(parameters)),
            expires_at=self.clock() + 600,
            epoch=epoch,
            selected=selected,
        )
        self._pending[state] = attempt
        return attempt

    def consume(self, attempt: AuthorizationAttempt, query: str) -> tuple[str, str]:
        """Consume state once; reject callback/client confusion before exchange."""
        try:
            if len(query) > 8192:
                raise ValueError("callback too large")
            values = parse_qs(
                query, keep_blank_values=True, strict_parsing=True, max_num_fields=8
            )
            if any(len(items) != 1 for items in values.values()):
                raise ValueError("duplicate callback fields")
            if any(
                name
                not in {
                    "code",
                    "state",
                    "client_id",
                    "scope",
                    "error",
                    "error_description",
                }
                for name in values
            ):
                raise ValueError("unknown callback field")
            returned_state = values.get("state", [""])[0]
            expected_state = attempt.state.get_secret_value()
            if not hmac.compare_digest(returned_state, expected_state):
                raise AuthFailure("state_mismatch")
            saved = self._pending.pop(expected_state, None)
            if saved is not attempt or attempt.expires_at <= self.clock():
                raise AuthFailure("invalid_callback")
            if "error" in values:
                raise AuthFailure("consent_denied")
            code = values.get("code", [""])[0]
            client_id = values.get(
                "client_id", [attempt.selected.client_id if attempt.selected else ""]
            )[0]
            if (
                not code
                or not client_id
                or client_id == "dynamic_agent_client"
                or len(client_id) > 256
            ):
                raise AuthFailure("invalid_callback")
            if attempt.selected is not None and client_id != attempt.selected.client_id:
                raise AuthFailure("invalid_callback")
            return code, client_id
        except (ValueError, IndexError, TypeError):
            raise AuthFailure("invalid_callback") from None

    def is_pending(self, attempt: AuthorizationAttempt) -> bool:
        """Tell the local listener whether an invalid callback consumed its attempt."""
        return (
            self._pending.get(attempt.state.get_secret_value()) is attempt
            and attempt.expires_at > self.clock()
        )

    async def exchange(
        self, attempt: AuthorizationAttempt, code: str, client_id: str
    ) -> tuple[TokenResponse, dict[str, JsonValue]]:
        """Exchange one code using the same URI, resource and PKCE verifier."""
        metadata = await self.discovery()
        payload = await self.request(
            "POST",
            metadata.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": attempt.verifier.get_secret_value(),
                "redirect_uri": attempt.redirect_uri,
                "resource": RESOURCE,
            },
        )
        try:
            token = TokenResponse.model_validate(payload)
            if (
                token.token_type.lower() != "bearer"
                or token.id_token is None
                or token.scope is None
            ):
                raise AuthFailure("invalid_response")
        except ValueError:
            raise AuthFailure("invalid_response") from None
        return token, await self.request("GET", metadata.jwks_uri)
