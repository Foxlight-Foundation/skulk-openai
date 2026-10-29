"""OIDC signature/claim validation with bounded issuer-owned JWKS input."""

import hmac
import json
from typing import final

import jwt
from pydantic import ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from skulk_inference_reference.contracts import FrozenContract

from .auth_contracts import ISSUER, AuthFailure

_OBJECT = TypeAdapter(dict[str, JsonValue])


@final
class Identity(FrozenContract):
    """Validated issuer/subject facts; never returned in public capability status."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")
    iss: str
    sub: str = Field(min_length=1, max_length=512, repr=False)
    aud: str | tuple[str, ...]
    exp: float
    iat: float
    nonce: str | None = Field(default=None, repr=False)
    azp: str | None = None


def verify_identity(
    encoded: str,
    jwks: dict[str, JsonValue],
    *,
    client_id: str,
    nonce: str | None,
    subject: str | None = None,
) -> Identity:
    """Verify issuer signature, audience, time and optional one-attempt nonce.

    Keys come only from the fixed issuer's discovery/JWKS request. Header URLs
    and embedded keys never initiate requests or override the selected issuer.
    """
    if len(encoded) > 32768:
        raise AuthFailure("identity_invalid")
    try:
        header = _OBJECT.validate_python(jwt.get_unverified_header(encoded))
        if any(name in header for name in ("jku", "x5u", "jwk")):
            raise AuthFailure("identity_invalid")
        algorithm = header.get("alg")
        key_id = header.get("kid")
        if algorithm not in ("RS256", "ES256") or not isinstance(key_id, str):
            raise AuthFailure("identity_invalid")
        values = jwks.get("keys")
        if not isinstance(values, list) or len(values) > 16:
            raise AuthFailure("identity_invalid")
        candidates = [
            item
            for item in values
            if isinstance(item, dict) and item.get("kid") == key_id
        ]
        if len(candidates) != 1:
            raise AuthFailure("identity_invalid")
        key_data = candidates[0]
        if (
            key_data.get("use", "sig") != "sig"
            or key_data.get("alg", algorithm) != algorithm
        ):
            raise AuthFailure("identity_invalid")
        key = jwt.PyJWK.from_dict(key_data, algorithm=algorithm)
        claims = jwt.decode(
            encoded,
            key.key,
            algorithms=[algorithm],
            issuer=ISSUER,
            audience=client_id,
            leeway=5,
            options={"require": ["iss", "sub", "aud", "exp", "iat"]},
        )
        identity = Identity.model_validate_json(json.dumps(claims, allow_nan=False))
        if nonce is not None and (
            identity.nonce is None or not hmac.compare_digest(identity.nonce, nonce)
        ):
            raise AuthFailure("identity_invalid")
        if identity.azp is not None and identity.azp != client_id:
            raise AuthFailure("identity_invalid")
        if (
            isinstance(identity.aud, tuple)
            and len(identity.aud) > 1
            and identity.azp != client_id
        ):
            raise AuthFailure("identity_invalid")
        if subject is not None and not hmac.compare_digest(identity.sub, subject):
            raise AuthFailure("identity_invalid")
        return identity
    except (jwt.PyJWTError, ValueError, ValidationError, TypeError):
        raise AuthFailure("identity_invalid") from None
