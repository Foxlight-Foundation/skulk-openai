"""Private-to-the-capability OAuth/session values; never descriptor or State data."""

from typing import Literal, final

from pydantic import ConfigDict, Field, SecretStr

from skulk_inference_reference.contracts import FrozenContract

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"

AuthCode = Literal[
    "invalid_callback",
    "state_mismatch",
    "consent_denied",
    "identity_invalid",
    "invalid_response",
    "provider_unavailable",
    "session_revoked",
    "sign_in_required",
    "permission_missing",
    "stale_authorization",
    "credentials_unavailable",
]


class AuthFailure(Exception):
    """Sanitized control outcome; upstream bodies and tokens are never retained."""

    def __init__(self, code: AuthCode) -> None:
        """Keep only a stable error code for operator-facing recovery."""
        super().__init__(code)
        self.code = code


@final
class TokenSet(FrozenContract):
    """Owner-local bearer credentials; ordinary model serialization masks secrets."""

    access_token: SecretStr = Field(repr=False, min_length=1, max_length=16384)
    refresh_token: SecretStr | None = Field(default=None, repr=False, max_length=16384)
    id_token: SecretStr = Field(repr=False, min_length=1, max_length=32768)
    scopes: tuple[str, ...] = Field(max_length=32)
    expires_at: float = Field(gt=0)
    refresh_at: float = Field(ge=0)


@final
class Registration(FrozenContract):
    """One verified subject/client pairing; the client preserves workspace binding."""

    registration_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    subject: str = Field(min_length=1, max_length=512, repr=False)
    client_id: str = Field(min_length=1, max_length=256, repr=False)
    generation: int = Field(ge=1)
    enabled: bool
    tokens: TokenSet | None = Field(default=None, repr=False)


@final
class ConnectionStatus(FrozenContract):
    """Content-free cached status, safe for a capability's management interface."""

    state: Literal["disconnected", "identity_only", "authorized", "expired"]
    registration_id: str | None = None
    generation: int | None = None


@final
class TokenResponse(FrozenContract):
    """Bounded token endpoint response; tolerate additive upstream metadata."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")
    access_token: SecretStr = Field(repr=False, min_length=1, max_length=16384)
    refresh_token: SecretStr | None = Field(default=None, repr=False, max_length=16384)
    id_token: SecretStr | None = Field(default=None, repr=False, max_length=32768)
    token_type: str = Field(max_length=16)
    expires_in: int = Field(ge=1, le=86400)
    scope: str | None = Field(default=None, max_length=2048)
    earliest_refresh_at: float | None = Field(default=None, ge=0)
