"""Account-free continuation semantics, not a published SDK or gateway ABI.

Authentication, signature verification, atomic storage and provider checkpoints
are injected ownership boundaries. This module performs no I/O and authenticates
nobody. Existing chat contracts do not negotiate or call it implicitly.
"""

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Annotated, Literal, Never, Self, final

from pydantic import Field, JsonValue, TypeAdapter, model_validator

from .contracts import FrozenContract, Identifier
from .input_body import InputBody

CONTINUATION_CONTRACT = "chat.continuation@0.1.0"
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Handle = Annotated[str, Field(pattern=r"^ct1_[a-f0-9]{64}$")]
_REQUEST = TypeAdapter(dict[str, JsonValue])
_HANDLE = TypeAdapter(Handle)
_EMPTY_HISTORY = hashlib.sha256(b"skulk.transcript.v1\0").hexdigest()


@final
class CallerScope(FrozenContract):
    """Opaque identity resolved by an authenticator, never by chat request JSON."""

    authority: Identifier = Field(description="Stable authentication namespace.")
    subject: Identifier = Field(description="Opaque caller ID within that namespace.")
    generation: int = Field(ge=1, description="Credential revocation generation.")


@final
@dataclass(frozen=True)
class VerifiedCaller:
    """Trusted adapter input, deliberately lacking a JSON deserialization API.

    Construct only after independent authentication and authorization. A gateway
    signing key, a client-chosen principal, or possession of a handle is not an
    authenticator. Expiry and revocation must be rechecked by the enforcing owner.
    """

    scope: CallerScope
    expires_at: float


@final
class ContinuationTarget(FrozenContract):
    """Exact destination pinned across a stream, tool loop and checkpoint."""

    target_node: Identifier
    callable_id: Identifier
    descriptor_revision: str = Field(pattern=r"^[a-f0-9]{16}$")
    model: Identifier


@final
@dataclass(frozen=True)
class OwnerContext:
    """Current trusted capability-local facts; never serialize into a catalog.

    Account and policy epochs are opaque local revisions, not upstream account
    identifiers. The owner supplies fresh snapshots at reservation, dispatch and
    completion, including after restart. Disabled or expired authority refuses
    work regardless of any otherwise valid checkpoint.
    """

    target: ContinuationTarget
    account_epoch: str
    policy_epoch: str
    enabled: bool
    expires_at: float


@final
class TranscriptFingerprint(FrozenContract):
    """Exact ordered public message history without retaining its contents."""

    count: int = Field(ge=0, le=4096)
    sha256: Digest


@final
class ContinuationRequest(FrozenContract):
    """Explicit opt-in metadata bound by the gateway to one exact media body.

    A trusted gateway authenticates the caller separately and signs both caller
    scope and this request. No field here grants caller or account authority.
    Full Chat Completions history remains required in this first reference;
    suffix-only requests and implicit conversation discovery are unsupported.
    """

    contract: Literal["chat.continuation@0.1.0"] = CONTINUATION_CONTRACT
    mode: Literal["start", "continue"]
    conversation_id: Digest = Field(description="Explicit random conversation ID.")
    handle: Handle | None = Field(default=None, description="Opaque scoped pointer.")
    target: ContinuationTarget
    body: InputBody
    nonce: Digest = Field(description="One-use attempt ID, durably fenced by owner.")
    expires_at: float = Field(gt=0, description="Short admission deadline.")

    @model_validator(mode="after")
    def negotiated_shape(self) -> Self:
        """Prevent a missing handle from silently starting another conversation."""
        if (self.mode == "continue") != (self.handle is not None):
            raise ValueError("continuation mode and handle disagree")
        return self


@final
class ContinuationReceipt(FrozenContract):
    """Content-free result published only after an atomic checkpoint commit."""

    contract: Literal["chat.continuation@0.1.0"] = CONTINUATION_CONTRACT
    conversation_id: Digest
    handle: Handle
    history: TranscriptFingerprint
    request_nonce: Digest
    expires_at: float = Field(gt=0)


@final
@dataclass(frozen=True)
class Binding:
    """Capability-private scope; the provider's account binding stays local."""

    caller: CallerScope
    conversation_id: str
    target: ContinuationTarget
    account_epoch: str
    policy_epoch: str


@final
@dataclass(frozen=True)
class Attempt:
    """Exact reserved request and its verified full input-history fingerprint."""

    request: ContinuationRequest
    input_history: TranscriptFingerprint


@final
@dataclass(frozen=True)
class ContinuationState:
    """Immutable local metadata for a compare-and-swap storage adapter.

    Canonical provider items are intentionally absent. A capability must retain
    those privately and commit them atomically with this metadata. Persist the
    dispatched phase before contacting the provider. An uncertain dispatched
    attempt may never be automatically replayed, even after a process restart.
    """

    binding: Binding
    version: int
    phase: Literal["ready", "reserved", "dispatched", "interrupted"]
    history: TranscriptFingerprint
    handle: str | None
    expires_at: float
    committed_nonce: str | None = None
    attempt: Attempt | None = None


FailureCode = Literal[
    "authority_unavailable",
    "scope_mismatch",
    "expired",
    "invalid_body",
    "history_mismatch",
    "conversation_exists",
    "handle_unavailable",
    "busy",
    "recovery_required",
    "invalid_transition",
]


class ContinuationFailure(Exception):
    """Bounded content-free failure; no request, handle or provider error text."""

    def __init__(self, code: FailureCode) -> None:
        """Keep only a stable classification for explicit caller recovery."""
        super().__init__(code)
        self.code = code


def append_message(
    history: TranscriptFingerprint, message: dict[str, JsonValue]
) -> TranscriptFingerprint:
    """Hash canonical JSON with ordered messages and unambiguous length framing.

    Only object-key order is normalized. A client that edits, drops or rewrites
    earlier content must explicitly start a new conversation; similar prefixes
    and human-readable summaries are never evidence of compatibility.
    """
    raw = json.dumps(
        message,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(
        bytes.fromhex(history.sha256) + len(raw).to_bytes(8, "big") + raw
    ).hexdigest()
    return TranscriptFingerprint(count=history.count + 1, sha256=digest)


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate request field")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> Never:
    raise ValueError("nonfinite JSON value")


def _history(
    raw: bytes, prefix_count: int
) -> tuple[TranscriptFingerprint, TranscriptFingerprint]:
    try:
        value: object = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
        request = _REQUEST.validate_python(value)
        messages = request.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 4095:
            raise ValueError("invalid history")
        if prefix_count >= len(messages):
            raise ValueError("new input required")
        history = TranscriptFingerprint(count=0, sha256=_EMPTY_HISTORY)
        prefix = history
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("invalid message")
            history = append_message(history, message)
            if history.count == prefix_count:
                prefix = history
        return history, prefix
    except (ValueError, RecursionError):
        raise ContinuationFailure("invalid_body") from None


def _authority(caller: VerifiedCaller | None, owner: OwnerContext, now: float) -> None:
    if caller is None or not owner.enabled:
        raise ContinuationFailure("authority_unavailable")
    if not now < min(caller.expires_at, owner.expires_at):
        raise ContinuationFailure("expired")


def _binding(
    request: ContinuationRequest, caller: VerifiedCaller, owner: OwnerContext
) -> Binding:
    return Binding(
        caller.scope,
        request.conversation_id,
        request.target,
        owner.account_epoch,
        owner.policy_epoch,
    )


def _current(
    state: ContinuationState,
    caller: VerifiedCaller | None,
    owner: OwnerContext,
    now: float,
) -> None:
    _authority(caller, owner, now)
    if caller is None:  # Narrow the type after the authority check.
        raise ContinuationFailure("authority_unavailable")
    binding = state.binding
    if (
        binding.caller != caller.scope
        or binding.target != owner.target
        or binding.account_epoch != owner.account_epoch
        or binding.policy_epoch != owner.policy_epoch
    ):
        raise ContinuationFailure("scope_mismatch")
    if state.expires_at <= now:
        raise ContinuationFailure("expired")


def reserve(
    request: ContinuationRequest,
    raw: bytes,
    *,
    caller: VerifiedCaller | None,
    owner: OwnerContext,
    existing: ContinuationState | None,
    now: float,
    lifetime_seconds: int = 3600,
) -> ContinuationState:
    """Validate one explicit attempt before an adapter atomically reserves it.

    The adapter must independently verify signatures, fence used nonces, and
    compare-and-swap the returned version against the exact state read. No
    provider effect is permitted before that transaction succeeds. Body bytes
    must already be completely received on the bounded invocation media lane.
    """
    _authority(caller, owner, now)
    if caller is None:
        raise ContinuationFailure("authority_unavailable")
    if request.target != owner.target:
        raise ContinuationFailure("scope_mismatch")
    if not now < request.expires_at <= now + 60 or not 1 <= lifetime_seconds <= 86400:
        raise ContinuationFailure("expired")
    if (
        len(raw) != request.body.size_bytes
        or hashlib.sha256(raw).hexdigest() != request.body.sha256
    ):
        raise ContinuationFailure("invalid_body")
    binding = _binding(request, caller, owner)
    if request.mode == "start":
        if existing is not None:
            raise ContinuationFailure("conversation_exists")
        existing = ContinuationState(
            binding=binding,
            version=0,
            phase="ready",
            history=TranscriptFingerprint(count=0, sha256=_EMPTY_HISTORY),
            handle=None,
            expires_at=min(now + lifetime_seconds, caller.expires_at),
        )
    else:
        if existing is None:
            raise ContinuationFailure("handle_unavailable")
        _current(existing, caller, owner, now)
        if existing.binding != binding:
            raise ContinuationFailure("scope_mismatch")
        if request.handle != existing.handle:
            raise ContinuationFailure("handle_unavailable")
    if existing.phase == "interrupted":
        raise ContinuationFailure("recovery_required")
    if existing.phase != "ready":
        raise ContinuationFailure("busy")
    history, prefix = _history(raw, existing.history.count)
    if prefix != existing.history:
        raise ContinuationFailure("history_mismatch")
    return replace(
        existing,
        version=existing.version + 1,
        phase="reserved",
        attempt=Attempt(request, history),
    )


def dispatch(
    state: ContinuationState,
    *,
    caller: VerifiedCaller | None,
    owner: OwnerContext,
    now: float,
) -> ContinuationState:
    """Recheck live authority and mark uncertainty durably before provider I/O."""
    _current(state, caller, owner, now)
    if state.phase != "reserved" or state.attempt is None:
        raise ContinuationFailure("invalid_transition")
    if state.attempt.request.expires_at <= now:
        raise ContinuationFailure("expired")
    return replace(state, version=state.version + 1, phase="dispatched")


def complete(
    state: ContinuationState,
    assistant_message: dict[str, JsonValue],
    *,
    new_handle: str,
    caller: VerifiedCaller | None,
    owner: OwnerContext,
    now: float,
) -> tuple[ContinuationState, ContinuationReceipt]:
    """Prepare a commit after verified provider completion and canonical output.

    The adapter validates output, retains canonical provider items privately and
    atomically persists them with this state before publishing the receipt. A
    delta or incomplete stream must never call this function. Lost delivery may
    recover this receipt by authenticated attempt lookup, never by resubmitting
    inference. Expiring admission deadlines do not truncate an admitted stream.
    """
    _current(state, caller, owner, now)
    if state.phase != "dispatched" or state.attempt is None:
        raise ContinuationFailure("invalid_transition")
    handle = _HANDLE.validate_python(new_handle)
    if handle == state.handle or assistant_message.get("role") != "assistant":
        raise ContinuationFailure("invalid_transition")
    history = append_message(state.attempt.input_history, assistant_message)
    receipt = ContinuationReceipt(
        conversation_id=state.binding.conversation_id,
        handle=handle,
        history=history,
        request_nonce=state.attempt.request.nonce,
        expires_at=state.expires_at,
    )
    return (
        replace(
            state,
            version=state.version + 1,
            phase="ready",
            history=history,
            handle=handle,
            committed_nonce=state.attempt.request.nonce,
            attempt=None,
        ),
        receipt,
    )


def interrupt(state: ContinuationState) -> ContinuationState | None:
    """Preserve the previous checkpoint without permitting ambiguous resubmission.

    A pre-dispatch reservation can be released, but its nonce stays spent. Once
    dispatch is persisted, recovery requires explicit abandonment and a new
    conversation using retained full history. The adapter uses compare-and-swap
    so late cleanup can never overwrite a newer attempt or commit.
    """
    if state.phase == "reserved":
        if state.handle is None:
            return None
        return replace(state, version=state.version + 1, phase="ready", attempt=None)
    if state.phase == "dispatched":
        return replace(state, version=state.version + 1, phase="interrupted")
    raise ContinuationFailure("invalid_transition")


def authorize_receipt(
    state: ContinuationState,
    receipt: ContinuationReceipt,
    *,
    caller: VerifiedCaller | None,
    owner: OwnerContext,
    now: float,
) -> None:
    """Authorize content-free recovery of the current committed receipt only."""
    _current(state, caller, owner, now)
    if (
        state.phase != "ready"
        or receipt.conversation_id != state.binding.conversation_id
        or receipt.handle != state.handle
        or receipt.history != state.history
        or receipt.expires_at != state.expires_at
        or receipt.request_nonce != state.committed_nonce
    ):
        raise ContinuationFailure("handle_unavailable")
