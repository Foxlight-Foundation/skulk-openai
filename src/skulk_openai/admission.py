"""Capability-owned caller grants; no provider secrets or SDK authority claims."""

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from typing import final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import Field, SecretStr

from skulk_inference_reference.contracts import (
    Authority,
    FrozenContract,
    GenerationRequest,
    Identifier,
    Offer,
    Requirements,
)
from skulk_inference_reference.routing import rejection_reasons

from .auth_contracts import PLAN_SCOPE, AuthFailure, TokenSet
from .credentials import CredentialStore
from .provider import CatalogSnapshot


@final
class Grant(FrozenContract):
    """One expiring, finite owner delegation bound to an account generation."""

    grant_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    public_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    registration_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    generation: int = Field(ge=1)
    models: tuple[Identifier, ...] = Field(min_length=1, max_length=8)
    maximum_output_characters: int = Field(ge=1, le=4096)
    maximum_attempts: int = Field(ge=1, le=64)
    expires_at: float = Field(gt=0)


@final
class Invocation(FrozenContract):
    """A signed target, input and hard policy; contains no account credential."""

    target_node: Identifier
    callable_id: Identifier
    descriptor_revision: str = Field(pattern=r"^[a-f0-9]{16}$")
    catalog_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    model: Identifier
    request: GenerationRequest
    requirements: Requirements
    nonce: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: float = Field(gt=0)

    def canonical(self, grant_id: str) -> bytes:
        """Canonicalize unordered policy sets identically across caller processes."""
        value = {
            **self.model_dump(mode="json"),
            "requirements": {
                **self.requirements.model_dump(mode="json"),
                "allowed_domains": sorted(self.requirements.allowed_domains),
                "allowed_usage": sorted(self.requirements.allowed_usage),
            },
        }
        return json.dumps(
            {"grant_id": grant_id, "invocation": value},
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()


@final
class SignedInvocation(FrozenContract):
    """Short-lived one-use authorization proof, confidential on the fabric hop."""

    grant_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    invocation: Invocation
    signature: str = Field(pattern=r"^[a-f0-9]{128}$")


@final
class CallerCredential(FrozenContract):
    """Caller-held signing material; it cannot refresh or retrieve provider tokens."""

    grant: Grant
    private_key: SecretStr = Field(repr=False, min_length=64, max_length=64)

    def sign(self, invocation: Invocation) -> SignedInvocation:
        """Sign exact request/target/policy; no HTTP or automatic request replay."""
        key = Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(self.private_key.get_secret_value())
        )
        return SignedInvocation(
            grant_id=self.grant.grant_id,
            invocation=invocation,
            signature=key.sign(invocation.canonical(self.grant.grant_id)).hex(),
        )


def catalog_offer(
    catalog: CatalogSnapshot, model: str, *, callable_id: str, descriptor_revision: str
) -> Offer:
    """Derive conservative provider facts; never trust caller-supplied offer labels."""
    if model not in {item.slug for item in catalog.models}:
        raise AuthFailure("permission_missing")
    identity = hashlib.sha256(
        json.dumps([catalog.registration_id, model]).encode()
    ).hexdigest()
    return Offer(
        offer_id="openai." + identity,
        provider="openai",
        model=model,
        callable_id=callable_id,
        descriptor_revision=descriptor_revision,
        offer_revision=int(catalog.revision[:16], 16) + 1,
        domain="external",
        usage="unknown",
        context_tokens=None,
        ready=True,
        expires_at=catalog.expires_at,
    )


class GrantStore:
    """Reserve nonce and attempt budget atomically with account-generation checks.

    Successful transaction commit is the admission point. Disconnect/revocation
    before it rejects the call; work admitted earlier may already be in flight.
    No prompt, response or request digest is stored in this journal.
    """

    def __init__(
        self, credentials: CredentialStore, *, clock: Callable[[], float] = time.time
    ) -> None:
        """Use the same protected database and write serialization as disconnect."""
        self.credentials = credentials
        self.clock = clock
        with credentials.transaction() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS grants (
                    id TEXT PRIMARY KEY, policy TEXT NOT NULL,
                    remaining INTEGER NOT NULL, enabled INTEGER NOT NULL,
                    expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS grant_nonces (
                    grant_id TEXT NOT NULL, nonce TEXT NOT NULL,
                    PRIMARY KEY (grant_id, nonce)
                );
            """)

    @staticmethod
    def _account(
        connection: sqlite3.Connection, registration_id: str, generation: int
    ) -> None:
        selected = connection.execute(
            "SELECT a.generation, a.enabled, a.tokens FROM accounts a "
            "JOIN metadata m ON m.key='active' AND m.value=a.registration_id "
            "WHERE a.registration_id=?",
            (registration_id,),
        ).fetchone()
        if (
            selected is None
            or int(selected[0]) != generation
            or not selected[1]
            or selected[2] is None
            or PLAN_SCOPE not in TokenSet.model_validate_json(str(selected[2])).scopes
        ):
            raise AuthFailure("permission_missing")

    def issue(
        self,
        catalog: CatalogSnapshot,
        *,
        models: tuple[str, ...],
        maximum_attempts: int,
        maximum_output_characters: int,
        lifetime_seconds: float,
        allow_external_unknown_usage: bool,
    ) -> CallerCredential:
        """Explicitly delegate up to sixty-four attempts for at most one hour."""
        now = self.clock()
        if (
            not allow_external_unknown_usage
            or not 0 < lifetime_seconds <= 3600
            or catalog.expires_at <= now
            or not set(models).issubset(item.slug for item in catalog.models)
        ):
            raise AuthFailure("permission_missing")
        key = Ed25519PrivateKey.generate()
        grant = Grant(
            grant_id=secrets.token_hex(16),
            public_key=key.public_key().public_bytes_raw().hex(),
            registration_id=catalog.registration_id,
            generation=catalog.generation,
            models=tuple(sorted(set(models))),
            maximum_attempts=maximum_attempts,
            maximum_output_characters=maximum_output_characters,
            expires_at=now + lifetime_seconds,
        )
        with self.credentials.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._account(connection, grant.registration_id, grant.generation)
            connection.execute(
                "DELETE FROM grant_nonces WHERE grant_id IN "
                "(SELECT id FROM grants WHERE expires<=?)",
                (now,),
            )
            connection.execute("DELETE FROM grants WHERE expires<=?", (now,))
            if connection.execute("SELECT COUNT(*) FROM grants").fetchone()[0] >= 32:
                raise AuthFailure("permission_missing")
            connection.execute(
                "INSERT INTO grants VALUES (?, ?, ?, 1, ?)",
                (
                    grant.grant_id,
                    grant.model_dump_json(),
                    maximum_attempts,
                    grant.expires_at,
                ),
            )
        return CallerCredential(
            grant=grant, private_key=SecretStr(key.private_bytes_raw().hex())
        )

    def revoke(self, grant_id: str) -> None:
        """Withdraw new admission immediately; keep replay fences until expiry."""
        with self.credentials.transaction() as connection:
            connection.execute("UPDATE grants SET enabled=0 WHERE id=?", (grant_id,))

    def admit(
        self,
        signed: SignedInvocation,
        *,
        catalog: CatalogSnapshot,
        target_node: str,
        callable_id: str,
        descriptor_revision: str,
    ) -> None:
        """Reject forgery, stale targets, hard denials and replay before submission."""
        now, invocation = self.clock(), signed.invocation
        if (
            invocation.target_node != target_node
            or invocation.callable_id != callable_id
            or invocation.descriptor_revision != descriptor_revision
            or invocation.catalog_revision != catalog.revision
            or not now < invocation.expires_at <= now + 60
        ):
            raise AuthFailure("permission_missing")
        offer = catalog_offer(
            catalog,
            invocation.model,
            callable_id=callable_id,
            descriptor_revision=descriptor_revision,
        )
        try:
            with self.credentials.transaction() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT policy, remaining, enabled FROM grants WHERE id=?",
                    (signed.grant_id,),
                ).fetchone()
                if row is None or int(row[1]) <= 0 or not row[2]:
                    raise AuthFailure("permission_missing")
                grant = Grant.model_validate_json(str(row[0]))
                self._account(connection, grant.registration_id, grant.generation)
                if (
                    grant.registration_id != catalog.registration_id
                    or grant.generation != catalog.generation
                    or invocation.model not in grant.models
                    or invocation.expires_at > grant.expires_at
                ):
                    raise AuthFailure("permission_missing")
                Ed25519PublicKey.from_public_bytes(
                    bytes.fromhex(grant.public_key)
                ).verify(
                    bytes.fromhex(signed.signature),
                    invocation.canonical(signed.grant_id),
                )
                authority = Authority(
                    principal=grant.grant_id,
                    revision=grant.generation,
                    allowed_offers=frozenset({offer.offer_id}),
                    allowed_domains=frozenset({"external"}),
                    allowed_usage=frozenset({"unknown"}),
                    maximum_output_characters=grant.maximum_output_characters,
                    expires_at=grant.expires_at,
                )
                if rejection_reasons(
                    offer, invocation.request, invocation.requirements, authority, now
                ):
                    raise AuthFailure("permission_missing")
                connection.execute(
                    "INSERT INTO grant_nonces VALUES (?, ?)",
                    (grant.grant_id, invocation.nonce),
                )
                connection.execute(
                    "UPDATE grants SET remaining=remaining-1 WHERE id=?",
                    (grant.grant_id,),
                )
        except (InvalidSignature, sqlite3.IntegrityError):
            raise AuthFailure("permission_missing") from None
