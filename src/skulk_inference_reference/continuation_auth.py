"""Caller-key proofs for the experimental provider-neutral continuation contract.

The destination resolves the public key from its owner-issued registry. Neither
a self-declared principal nor the public key accompanying a request grants access.
"""

import json
from typing import Literal, final

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import Field, SecretStr

from .continuation import ContinuationRequest, ContinuationTarget, Digest
from .contracts import FrozenContract, Identifier


class ClientRegistration(FrozenContract):
    """Owner-approved public key, explicit model scope and credential deadline."""

    public_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    models: tuple[Identifier, ...] = Field(min_length=1, max_length=128)
    expires_at: float = Field(gt=0)


@final
class RegisteredClient(ClientRegistration):
    """Public enrollment result; it contains no account or client private key."""

    caller_id: str = Field(pattern=r"^[a-f0-9]{32}$")


@final
class ClientEnrollment(FrozenContract):
    """Public approved scope and exact managed destination, with no account data."""

    client: RegisteredClient
    target: ContinuationTarget


@final
class ContinuationKey(FrozenContract):
    """Client-generated signing key, created without a provider account or SDK."""

    private_key: SecretStr = Field(repr=False, min_length=64, max_length=64)

    @classmethod
    def generate(cls) -> "ContinuationKey":
        """Create caller-only Ed25519 material locally without any network I/O."""
        return cls(
            private_key=SecretStr(
                Ed25519PrivateKey.generate().private_bytes_raw().hex()
            )
        )

    @property
    def public_key(self) -> str:
        """Return the shareable public key for an explicit owner approval."""
        return (
            Ed25519PrivateKey.from_private_bytes(
                bytes.fromhex(self.private_key.get_secret_value())
            )
            .public_key()
            .public_bytes_raw()
            .hex()
        )

    def bind(self, registration: RegisteredClient) -> "ContinuationCredential":
        """Accept an owner's caller ID only when its approved key matches ours."""
        if registration.public_key != self.public_key:
            raise ValueError("enrollment public key does not match client key")
        return ContinuationCredential(
            caller_id=registration.caller_id, private_key=self.private_key
        )


@final
class ReceiptQuery(FrozenContract):
    """Authenticate one current-receipt lookup without resubmitting inference."""

    contract: Literal["chat.continuation.receipt@0.1.0"] = (
        "chat.continuation.receipt@0.1.0"
    )
    conversation_id: Digest
    target: ContinuationTarget
    request_nonce: Digest
    nonce: Digest
    expires_at: float = Field(gt=0)


def canonical_proof(
    caller_id: str, request: ContinuationRequest | ReceiptQuery
) -> bytes:
    """Bind the registry identity, operation domain and complete request metadata."""
    return json.dumps(
        {"caller_id": caller_id, "request": request.model_dump(mode="json")},
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


@final
class ContinuationProof(FrozenContract):
    """A request signed by one independently registered client key."""

    caller_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request: ContinuationRequest
    signature: str = Field(pattern=r"^[a-f0-9]{128}$")


@final
class ReceiptProof(FrozenContract):
    """A separately domain-bound one-use receipt query, never a generation grant."""

    caller_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    request: ReceiptQuery
    signature: str = Field(pattern=r"^[a-f0-9]{128}$")


@final
class ContinuationCredential(FrozenContract):
    """Caller-only private signing material; contains no provider credential."""

    caller_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    private_key: SecretStr = Field(repr=False, min_length=64, max_length=64)

    def sign(self, request: ContinuationRequest) -> ContinuationProof:
        """Sign a bounded request without network access or automatic retries."""
        key = Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(self.private_key.get_secret_value())
        )
        return ContinuationProof(
            caller_id=self.caller_id,
            request=request,
            signature=key.sign(canonical_proof(self.caller_id, request)).hex(),
        )

    def receipt(self, query: ReceiptQuery) -> ReceiptProof:
        """Sign a content-free recovery lookup under a distinct operation domain."""
        key = Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(self.private_key.get_secret_value())
        )
        return ReceiptProof(
            caller_id=self.caller_id,
            request=query,
            signature=key.sign(canonical_proof(self.caller_id, query)).hex(),
        )
