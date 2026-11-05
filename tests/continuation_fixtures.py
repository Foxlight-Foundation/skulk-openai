"""Synthetic gateway, atomic-store and unrelated-provider conformance fixtures.

These mocks are not a production persistence or authentication implementation.
There is no network, account access, SDK dependency or encrypted real checkpoint.
"""

import copy
import json
import secrets
from dataclasses import dataclass, field
from typing import Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue

from skulk_inference_reference.continuation import (
    CallerScope,
    ContinuationFailure,
    ContinuationReceipt,
    ContinuationRequest,
    ContinuationState,
    ContinuationTarget,
    OwnerContext,
    VerifiedCaller,
    authorize_receipt,
    complete,
    dispatch,
    interrupt,
    reserve,
)
from skulk_inference_reference.input_body import InputBody, encode_body

type ConversationKey = tuple[str, str, str]
type NonceKey = tuple[str, str, str]


def conversation_key(caller: VerifiedCaller, conversation_id: str) -> ConversationKey:
    """Scope test storage by authenticated identity, never a shared signing key."""
    return caller.scope.authority, caller.scope.subject, conversation_id


@dataclass
class MemoryStore:
    """A single-threaded atomic-store fixture with a restartable durable snapshot."""

    states: dict[ConversationKey, ContinuationState] = field(default_factory=dict)
    spent: set[NonceKey] = field(default_factory=set)
    windows: dict[str, bytes] = field(default_factory=dict)
    receipts: dict[ConversationKey, ContinuationReceipt] = field(default_factory=dict)

    def swap(
        self,
        key: ConversationKey,
        expected: ContinuationState | None,
        replacement: ContinuationState | None,
    ) -> None:
        """Model one indivisible compare-and-swap, including removal on abort."""
        if self.states.get(key) != expected:
            raise ContinuationFailure("busy")
        if replacement is None:
            self.states.pop(key, None)
        else:
            self.states[key] = replacement

    def restarted(self) -> "MemoryStore":
        """Copy only simulated durable state, discarding all adapter call stacks."""
        return copy.deepcopy(self)


class FixtureProvider(Protocol):
    """Only generic owner facts and opaque private state cross the adapter seam."""

    calls: int
    received_windows: list[bytes | None]

    def context(self) -> OwnerContext:
        """Return current synthetic authority and destination facts."""
        ...

    def generate(self, window: bytes | None) -> bytes:
        """Consume and produce a capability-owned, provider-specific window."""
        ...


def fixture_context(kind: str) -> OwnerContext:
    """Provide distinct synthetic destination and private account epochs."""
    return OwnerContext(
        target=ContinuationTarget(
            target_node=f"fixture.{kind}",
            callable_id=f"{kind}.chat",
            descriptor_revision="a" * 16,
            model="synthetic-model",
        ),
        account_epoch=f"private-{kind}-account-generation-1",
        policy_epoch="private-policy-1",
        enabled=True,
        expires_at=10_000.0,
    )


@dataclass
class LocalCursorProvider:
    """An independent JSON cursor provider with no encrypted Responses items."""

    calls: int = 0
    received_windows: list[bytes | None] = field(default_factory=list)

    def context(self) -> OwnerContext:
        """Expose this fixture's local owner snapshot."""
        return fixture_context("local")

    def generate(self, window: bytes | None) -> bytes:
        """Resume only a local JSON cursor and advance its native revision."""
        self.calls += 1
        self.received_windows.append(window)
        prior = json.loads(window)["cursor"] if window else 0
        return json.dumps({"cursor": prior + 1, "dialect": "local"}).encode()


@dataclass
class SealedWindowProvider:
    """A synthetic remote provider with an incompatible binary window format."""

    calls: int = 0
    received_windows: list[bytes | None] = field(default_factory=list)

    def context(self) -> OwnerContext:
        """Expose this fixture's independent remote owner snapshot."""
        return fixture_context("remote")

    def generate(self, window: bytes | None) -> bytes:
        """Accept only this synthetic binary format; no real encryption is claimed."""
        self.calls += 1
        self.received_windows.append(window)
        if window is not None and not window.startswith(b"SEALED-REMOTE\xff"):
            raise ValueError("wrong fixture dialect")
        revision = int.from_bytes(window[-4:], "big") if window else 0
        return b"SEALED-REMOTE\xff" + (revision + 1).to_bytes(4, "big")


def fixture_provider(kind: Literal["local", "remote"]) -> FixtureProvider:
    """Select an unrelated implementation without changing the generic adapter."""
    return LocalCursorProvider() if kind == "local" else SealedWindowProvider()


@dataclass(frozen=True)
class SignedAttempt:
    """A test proof whose signature covers caller identity and the entire request."""

    caller: CallerScope
    request: ContinuationRequest
    signature: bytes

    def canonical(self) -> bytes:
        """Encode an unambiguous, bounded synthetic signing envelope."""
        return canonical_attempt(self.caller, self.request)


def canonical_attempt(caller: CallerScope, request: ContinuationRequest) -> bytes:
    """Keep identity within the proof rather than trusting unsigned body fields."""
    return json.dumps(
        {"caller": caller.model_dump(), "request": request.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


class GatewayFixture:
    """A mock authenticated gateway and capability adapter, with explicit opt-in.

    The synthetic delegation table represents independently resolved caller
    grants; verifying a gateway signature alone never grants a subject access.
    Normal full-history calls bypass continuation state completely.
    """

    def __init__(
        self, provider: FixtureProvider, store: MemoryStore | None = None
    ) -> None:
        """Create account-free identities and a shared gateway key for both."""
        self.provider = provider
        self.store = store or MemoryStore()
        self.key = Ed25519PrivateKey.generate()
        self.callers = {
            token: VerifiedCaller(
                CallerScope(authority="test-auth", subject=subject, generation=1),
                expires_at=8000.0,
            )
            for token, subject in (
                ("alice-token", "caller-a"),
                ("bob-token", "caller-b"),
            )
        }
        self.delegations = {c.scope: c for c in self.callers.values()}
        self.owner = provider.context()
        self.now = 1000.0

    def signed(
        self,
        raw: bytes,
        *,
        token: str = "alice-token",
        conversation: str = "1" * 64,
        receipt: ContinuationReceipt | None = None,
        nonce: str | None = None,
    ) -> SignedAttempt:
        """Authenticate the external credential before creating any signed claim."""
        caller = self.callers.get(token)
        if caller is None:
            raise ContinuationFailure("authority_unavailable")
        request = ContinuationRequest(
            mode="start" if receipt is None else "continue",
            conversation_id=conversation,
            handle=receipt.handle if receipt else None,
            target=self.owner.target,
            body=InputBody.from_bytes(raw),
            nonce=nonce or secrets.token_hex(32),
            expires_at=self.now + 30,
        )
        return SignedAttempt(
            caller.scope,
            request,
            self.key.sign(canonical_attempt(caller.scope, request)),
        )

    def authenticate(self, proof: SignedAttempt) -> VerifiedCaller:
        """Verify proof plus fresh caller grant before looking up any checkpoint."""
        try:
            self.key.public_key().verify(proof.signature, proof.canonical())
        except InvalidSignature:
            raise ContinuationFailure("authority_unavailable") from None
        caller = self.delegations.get(proof.caller)
        if caller is None:
            raise ContinuationFailure("authority_unavailable")
        return caller

    def admit(self, proof: SignedAttempt, raw: bytes) -> ContinuationState:
        """Simulate atomic nonce reservation and conversation compare-and-swap."""
        caller = self.authenticate(proof)
        key = conversation_key(caller, proof.request.conversation_id)
        nonce = (*key[:2], proof.request.nonce)
        if nonce in self.store.spent:
            raise ContinuationFailure("handle_unavailable")
        existing = self.store.states.get(key)
        state = reserve(
            proof.request,
            raw,
            caller=caller,
            owner=self.owner,
            existing=existing,
            now=self.now,
        )
        self.store.swap(key, existing, state)
        self.store.spent.add(nonce)
        return state

    def send(self, proof: SignedAttempt, state: ContinuationState) -> bytes:
        """Persist dispatched state before invoking the independent mock provider."""
        caller = self.authenticate(proof)
        key = conversation_key(caller, proof.request.conversation_id)
        if state.attempt is None or state.attempt.request != proof.request:
            raise ContinuationFailure("scope_mismatch")
        if state.handle is not None and state.handle not in self.store.windows:
            raise ContinuationFailure("handle_unavailable")
        window = self.store.windows[state.handle] if state.handle is not None else None
        dispatched = dispatch(state, caller=caller, owner=self.owner, now=self.now)
        self.store.swap(key, state, dispatched)
        return self.provider.generate(window)

    def finish(
        self,
        proof: SignedAttempt,
        output: dict[str, JsonValue],
        window: bytes,
    ) -> ContinuationReceipt:
        """Commit private state and receipt together before publishing completion."""
        caller = self.authenticate(proof)
        key = conversation_key(caller, proof.request.conversation_id)
        state = self.store.states[key]
        if state.attempt is None or state.attempt.request != proof.request:
            raise ContinuationFailure("scope_mismatch")
        following, receipt = complete(
            state,
            output,
            new_handle="ct1_" + secrets.token_hex(32),
            caller=caller,
            owner=self.owner,
            now=self.now,
        )
        # This no-await fixture models one atomic metadata/checkpoint transaction.
        # It deliberately makes no filesystem durability or encryption claim.
        self.store.swap(key, state, following)
        self.store.windows[receipt.handle] = window
        self.store.receipts[key] = receipt
        if state.handle is not None:
            del self.store.windows[state.handle]
        return receipt

    def run(
        self, proof: SignedAttempt, raw: bytes, output: dict[str, JsonValue]
    ) -> ContinuationReceipt:
        """Exercise reservation, provider dispatch and successful checkpoint commit."""
        state = self.admit(proof, raw)
        window = self.send(proof, state)
        return self.finish(proof, output, window)

    def abort(self, proof: SignedAttempt) -> None:
        """Model cleanup without erasing a committed checkpoint or nonce fence."""
        caller = self.authenticate(proof)
        key = conversation_key(caller, proof.request.conversation_id)
        state = self.store.states[key]
        if state.attempt is None or state.attempt.request != proof.request:
            raise ContinuationFailure("scope_mismatch")
        self.store.swap(key, state, interrupt(state))

    def recover(self, token: str, conversation: str) -> ContinuationReceipt:
        """Read only the current committed receipt under live scoped authority."""
        caller = self.callers[token]
        key = conversation_key(caller, conversation)
        if key not in self.store.receipts:
            raise ContinuationFailure("handle_unavailable")
        receipt = self.store.receipts[key]
        authorize_receipt(
            self.store.states[key],
            receipt,
            caller=self.delegations.get(caller.scope),
            owner=self.owner,
            now=self.now,
        )
        return receipt

    def full_history(self, raw: bytes) -> None:
        """A stock client never acquires or reuses a continuation implicitly."""
        assert b"messages" in raw
        self.provider.generate(None)


def body(messages: list[dict[str, JsonValue]]) -> bytes:
    """Encode exact public Chat Completions messages for digest/signature tests."""
    return encode_body({"messages": list(messages)})
