"""Account-free continuation conformance across unrelated provider dialects."""

import json
from dataclasses import replace
from typing import Literal

import pytest
from pydantic import JsonValue, ValidationError

from skulk_inference_reference.continuation import (
    CallerScope,
    ContinuationFailure,
    ContinuationReceipt,
    ContinuationRequest,
    ContinuationState,
    VerifiedCaller,
    authorize_receipt,
    complete,
    dispatch,
    interrupt,
    reserve,
)
from skulk_inference_reference.input_body import InputBody
from tests.continuation_fixtures import (
    GatewayFixture,
    SignedAttempt,
    body,
    conversation_key,
    fixture_provider,
)

USER: dict[str, JsonValue] = {"role": "user", "content": "Synthetic question"}
ANSWER: dict[str, JsonValue] = {"role": "assistant", "content": "Synthetic answer"}
FOLLOWUP: dict[str, JsonValue] = {"role": "user", "content": "Continue"}


@pytest.fixture(params=["local", "remote"])
def gateway(request: pytest.FixtureRequest) -> GatewayFixture:
    """Run every generic case against unrelated synthetic checkpoint formats."""
    kind: Literal["local", "remote"] = request.param
    return GatewayFixture(fixture_provider(kind))


def first_turn(gateway: GatewayFixture) -> tuple[SignedAttempt, ContinuationReceipt]:
    """Commit one turn without network, SDK, account or fleet access."""
    raw = body([USER])
    proof = gateway.signed(raw)
    return proof, gateway.run(proof, raw, ANSWER)


def current(gateway: GatewayFixture) -> ContinuationState:
    """Read the primary fixture's scoped local metadata."""
    return gateway.store.states[
        conversation_key(gateway.callers["alice-token"], "1" * 64)
    ]


def test_roundtrip_keeps_checkpoint_private_and_rotates_handle(
    gateway: GatewayFixture,
) -> None:
    _, receipt = first_turn(gateway)
    first_window = gateway.store.windows[receipt.handle]
    raw = body([USER, ANSWER, FOLLOWUP])
    following = gateway.run(gateway.signed(raw, receipt=receipt), raw, ANSWER)
    assert following.handle != receipt.handle
    assert following.history.count == 4
    assert gateway.provider.received_windows == [None, first_window]
    assert receipt.handle not in gateway.store.windows
    serialized = following.model_dump_json()
    for private in ("Synthetic", "SEALED", "LOCAL", "private-policy", "account"):
        assert private not in serialized
    assert len(serialized) < 600


def test_stock_full_history_calls_do_not_create_or_consume_state(
    gateway: GatewayFixture,
) -> None:
    gateway.full_history(body([USER]))
    gateway.full_history(body([USER, ANSWER, FOLLOWUP]))
    assert not gateway.store.states and not gateway.store.spent
    assert not gateway.store.windows and not gateway.store.receipts
    _, receipt = first_turn(gateway)
    before = gateway.store.restarted()
    gateway.full_history(body([USER, ANSWER, FOLLOWUP]))
    assert gateway.store == before
    assert gateway.provider.received_windows[-1] is None
    assert gateway.recover("alice-token", "1" * 64) == receipt


def test_same_gateway_key_and_identical_history_cannot_join_callers(
    gateway: GatewayFixture,
) -> None:
    _, receipt = first_turn(gateway)
    raw = body([USER, ANSWER, FOLLOWUP])
    proof = gateway.signed(raw, token="bob-token", receipt=receipt)
    with pytest.raises(ContinuationFailure, match="handle_unavailable"):
        gateway.admit(proof, raw)
    with pytest.raises(ContinuationFailure, match="handle_unavailable"):
        gateway.recover("bob-token", "1" * 64)
    assert gateway.provider.calls == 1


def test_conversations_with_identical_prefixes_remain_independent(
    gateway: GatewayFixture,
) -> None:
    _, receipt = first_turn(gateway)
    raw = body([USER])
    other = gateway.run(gateway.signed(raw, conversation="2" * 64), raw, ANSWER)
    assert other.history == receipt.history and other.handle != receipt.handle
    following = body([USER, ANSWER, FOLLOWUP])
    with pytest.raises(ContinuationFailure, match="handle_unavailable"):
        gateway.admit(
            gateway.signed(following, conversation="2" * 64, receipt=receipt), following
        )
    assert gateway.provider.calls == 2


@pytest.mark.parametrize("change", ["body", "identity", "model", "signature"])
def test_signed_body_and_identity_tampering_precede_provider_use(
    gateway: GatewayFixture, change: str
) -> None:
    raw = body([USER])
    proof = gateway.signed(raw)
    if change == "body":
        raw = raw.replace(b"question", b"tampered")
    elif change == "identity":
        proof = replace(proof, caller=gateway.callers["bob-token"].scope)
    elif change == "model":
        proof = replace(
            proof,
            request=proof.request.model_copy(
                update={
                    "target": proof.request.target.model_copy(update={"model": "other"})
                }
            ),
        )
    else:
        proof = replace(proof, signature=b"x" * 64)
    with pytest.raises(ContinuationFailure):
        gateway.admit(proof, raw)
    assert gateway.provider.calls == 0 and not gateway.store.states


def test_gateway_signature_does_not_grant_a_caller_authority(
    gateway: GatewayFixture,
) -> None:
    raw = body([USER])
    proof = gateway.signed(raw)
    del gateway.delegations[proof.caller]
    with pytest.raises(ContinuationFailure, match="authority_unavailable"):
        gateway.admit(proof, raw)
    with pytest.raises(ContinuationFailure, match="authority_unavailable"):
        reserve(
            proof.request,
            raw,
            caller=None,
            owner=gateway.owner,
            existing=None,
            now=1000,
        )
    assert gateway.provider.calls == 0


@pytest.mark.parametrize(
    "change", ["account", "policy", "model", "descriptor", "caller"]
)
@pytest.mark.parametrize("stage", ["resume", "dispatch", "commit", "recover"])
def test_binding_changes_refuse_reuse_at_every_boundary(
    gateway: GatewayFixture, change: str, stage: str
) -> None:
    _, receipt = first_turn(gateway)
    raw = body([USER, ANSWER, FOLLOWUP])
    proof = gateway.signed(raw, receipt=receipt)
    if stage in {"dispatch", "commit"}:
        state = gateway.admit(proof, raw)
        if stage == "commit":
            gateway.send(proof, state)
    if change == "account":
        gateway.owner = replace(gateway.owner, account_epoch="replacement-account")
    elif change == "policy":
        gateway.owner = replace(gateway.owner, policy_epoch="replacement-policy")
    elif change in {"model", "descriptor"}:
        field = "model" if change == "model" else "descriptor_revision"
        value = "other-model" if field == "model" else "b" * 16
        gateway.owner = replace(
            gateway.owner, target=gateway.owner.target.model_copy(update={field: value})
        )
    else:
        original = gateway.callers["alice-token"]
        newer = VerifiedCaller(
            original.scope.model_copy(update={"generation": 2}), expires_at=8000.0
        )
        gateway.delegations[original.scope] = newer
    with pytest.raises(ContinuationFailure, match="scope_mismatch"):
        if stage == "resume":
            gateway.admit(proof, raw)
        elif stage == "dispatch":
            gateway.send(proof, current(gateway))
        elif stage == "commit":
            gateway.finish(proof, ANSWER, b"uncommitted-checkpoint")
        else:
            gateway.recover("alice-token", "1" * 64)
    assert receipt.handle in gateway.store.windows
    assert gateway.provider.calls == (2 if stage == "commit" else 1)


@pytest.mark.parametrize("change", ["disabled", "expired", "revoked"])
def test_current_authority_is_required_before_dispatch(
    gateway: GatewayFixture, change: str
) -> None:
    raw = body([USER])
    proof = gateway.signed(raw)
    state = gateway.admit(proof, raw)
    if change == "disabled":
        gateway.owner = replace(gateway.owner, enabled=False)
    elif change == "expired":
        gateway.now += 31
    else:
        del gateway.delegations[proof.caller]
    with pytest.raises(ContinuationFailure):
        gateway.send(proof, state)
    assert gateway.provider.calls == 0


@pytest.mark.parametrize(
    "history", ["edit", "drop", "reorder", "suffix", "no_new_input"]
)
def test_history_mismatch_never_silently_restarts_or_reuses_state(
    gateway: GatewayFixture, history: str
) -> None:
    _, receipt = first_turn(gateway)
    messages = [USER, ANSWER, FOLLOWUP]
    if history == "edit":
        messages = [{**USER, "content": "Edited"}, ANSWER, FOLLOWUP]
    elif history == "drop":
        messages = [USER, FOLLOWUP]
    elif history == "reorder":
        messages = [ANSWER, USER, FOLLOWUP]
    elif history == "suffix":
        messages = [FOLLOWUP]
    else:
        messages = [USER, ANSWER]
    raw = body(messages)
    with pytest.raises(ContinuationFailure):
        gateway.admit(gateway.signed(raw, receipt=receipt), raw)
    assert gateway.provider.calls == 1 and receipt.handle in gateway.store.windows
    assert current(gateway).phase == "ready"


def test_replay_and_concurrent_attempts_cannot_duplicate_inference(
    gateway: GatewayFixture,
) -> None:
    original, receipt = first_turn(gateway)
    with pytest.raises(ContinuationFailure):
        gateway.admit(original, body([USER]))
    raw = body([USER, ANSWER, FOLLOWUP])
    one = gateway.signed(raw, receipt=receipt)
    two = gateway.signed(raw, receipt=receipt)
    state = gateway.admit(one, raw)
    with pytest.raises(ContinuationFailure, match="busy"):
        gateway.admit(two, raw)
    window = gateway.send(one, state)
    with pytest.raises(ContinuationFailure):
        gateway.send(one, state)
    following = gateway.finish(one, ANSWER, window)
    with pytest.raises(ContinuationFailure):
        gateway.admit(two, raw)
    assert gateway.provider.calls == 2
    assert following.handle in gateway.store.windows


@pytest.mark.parametrize("after_dispatch", [False, True])
def test_interruption_preserves_checkpoint_and_fences_uncertain_work(
    gateway: GatewayFixture, after_dispatch: bool
) -> None:
    _, receipt = first_turn(gateway)
    raw = body([USER, ANSWER, FOLLOWUP])
    proof = gateway.signed(raw, receipt=receipt)
    state = gateway.admit(proof, raw)
    if after_dispatch:
        gateway.send(proof, state)
    gateway.abort(proof)
    assert receipt.handle in gateway.store.windows
    with pytest.raises(ContinuationFailure):
        gateway.admit(proof, raw)
    following = gateway.signed(raw, receipt=receipt)
    if after_dispatch:
        with pytest.raises(ContinuationFailure, match="recovery_required"):
            gateway.admit(following, raw)
        assert gateway.provider.calls == 2
        restarted = gateway.signed(raw, conversation="2" * 64)
        gateway.run(restarted, raw, ANSWER)
        assert gateway.provider.received_windows[-1] is None
    else:
        gateway.run(following, raw, ANSWER)
        assert gateway.provider.calls == 2


def test_late_cleanup_cannot_overwrite_a_new_commit(gateway: GatewayFixture) -> None:
    _, receipt = first_turn(gateway)
    raw = body([USER, ANSWER, FOLLOWUP])
    proof = gateway.signed(raw, receipt=receipt)
    state = gateway.admit(proof, raw)
    window = gateway.send(proof, state)
    pending = current(gateway)
    following = gateway.finish(proof, ANSWER, window)
    key = conversation_key(gateway.callers["alice-token"], "1" * 64)
    with pytest.raises(ContinuationFailure, match="busy"):
        gateway.store.swap(key, pending, interrupt(pending))
    assert current(gateway).handle == following.handle


@pytest.mark.parametrize("phase", ["ready", "reserved", "dispatched", "committed"])
def test_process_restart_never_loses_nonce_fence_or_replays_provider(
    gateway: GatewayFixture, phase: str
) -> None:
    _, receipt = first_turn(gateway)
    raw = body([USER, ANSWER, FOLLOWUP])
    proof = gateway.signed(raw, receipt=receipt)
    if phase != "ready":
        state = gateway.admit(proof, raw)
        if phase in {"dispatched", "committed"}:
            window = gateway.send(proof, state)
            if phase == "committed":
                receipt = gateway.finish(proof, ANSWER, window)
    calls = gateway.provider.calls
    gateway.store = gateway.store.restarted()
    assert gateway.provider.calls == calls
    if phase in {"ready", "committed"}:
        assert gateway.recover("alice-token", "1" * 64) == receipt
    if phase != "ready":
        with pytest.raises(ContinuationFailure):
            gateway.admit(proof, raw)
    assert gateway.provider.calls == calls
    assert receipt.handle in gateway.store.windows


def test_lost_receipt_recovers_without_another_inference(
    gateway: GatewayFixture,
) -> None:
    proof, receipt = first_turn(gateway)
    gateway.store = gateway.store.restarted()
    assert gateway.recover("alice-token", "1" * 64) == receipt
    with pytest.raises(ContinuationFailure):
        gateway.admit(proof, body([USER]))
    assert gateway.provider.calls == 1
    forged = receipt.model_copy(update={"request_nonce": "f" * 64})
    with pytest.raises(ContinuationFailure):
        authorize_receipt(
            current(gateway),
            forged,
            caller=gateway.callers["alice-token"],
            owner=gateway.owner,
            now=gateway.now,
        )


def test_repeated_tool_turns_preserve_exact_public_ids_arguments_and_results(
    gateway: GatewayFixture,
) -> None:
    messages = [USER]
    receipt: ContinuationReceipt | None = None
    for number in range(4):
        tool: dict[str, JsonValue] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call-{number}",
                    "type": "function",
                    "function": {"name": "caller_tool", "arguments": '{"x":1}'},
                }
            ],
        }
        raw = body(messages)
        proof = gateway.signed(raw, receipt=receipt)
        receipt = gateway.run(proof, raw, tool)
        messages.extend(
            [
                tool,
                {"role": "tool", "tool_call_id": f"call-{number}", "content": "Result"},
            ]
        )
    assert receipt is not None
    assert receipt.history.count == 8 and gateway.provider.calls == 4
    messages[1] = {**messages[1], "tool_calls": []}
    raw = body(messages)
    with pytest.raises(ContinuationFailure, match="history_mismatch"):
        gateway.admit(gateway.signed(raw, receipt=receipt), raw)
    assert gateway.provider.calls == 4


@pytest.mark.parametrize("field", ["principal", "account_epoch", "provider_items"])
def test_untrusted_metadata_cannot_supply_authority_or_checkpoints(field: str) -> None:
    gateway = GatewayFixture(fixture_provider("local"))
    request = gateway.signed(body([USER])).request.model_dump(mode="json")
    request[field] = "injected"
    with pytest.raises(ValidationError):
        ContinuationRequest.model_validate_json(json.dumps(request))


@pytest.mark.parametrize(
    "handle", ["", "ct1_short", "ct1_" + "g" * 64, "https://x.test"]
)
def test_malformed_handles_are_not_fetchable_or_accepted(handle: str) -> None:
    gateway = GatewayFixture(fixture_provider("local"))
    request = gateway.signed(body([USER])).request.model_dump(mode="json")
    request.update(mode="continue", handle=handle)
    with pytest.raises(ValidationError):
        ContinuationRequest.model_validate_json(json.dumps(request))


@pytest.mark.parametrize(
    "raw",
    [
        b'{"messages":[],"messages":[]}',
        b'{"messages":[]} ',
        b"[]",
        b"\xff",
        b"{}",
        b'{"messages":[{"role":"user","content":"hi"}],"option":NaN}',
        b'{"messages":[{"role":"user","content":"hi"}],"option":Infinity}',
    ],
)
def test_ambiguous_and_missing_history_are_refused(raw: bytes) -> None:
    gateway = GatewayFixture(fixture_provider("local"))
    proof = gateway.signed(raw if len(raw) >= 2 else raw + b" ")
    with pytest.raises(ContinuationFailure, match="invalid_body"):
        gateway.admit(proof, raw)
    assert gateway.provider.calls == 0


def test_only_complete_dispatch_can_publish_receipt(gateway: GatewayFixture) -> None:
    raw = body([USER])
    proof = gateway.signed(raw)
    state = gateway.admit(proof, raw)
    with pytest.raises(ContinuationFailure, match="invalid_transition"):
        complete(
            state,
            ANSWER,
            new_handle="ct1_" + "9" * 64,
            caller=gateway.callers["alice-token"],
            owner=gateway.owner,
            now=gateway.now,
        )
    assert not gateway.store.receipts and not gateway.store.windows


def test_completed_stream_is_not_truncated_by_admission_deadline(
    gateway: GatewayFixture,
) -> None:
    raw = body([USER])
    proof = gateway.signed(raw)
    state = gateway.admit(proof, raw)
    window = gateway.send(proof, state)
    gateway.now += 45
    assert gateway.finish(proof, ANSWER, window).history.count == 2


def test_expired_checkpoint_requires_explicit_new_conversation(
    gateway: GatewayFixture,
) -> None:
    _, receipt = first_turn(gateway)
    gateway.now += 3601
    raw = body([USER, ANSWER, FOLLOWUP])
    with pytest.raises(ContinuationFailure, match="expired"):
        gateway.admit(gateway.signed(raw, receipt=receipt), raw)
    assert gateway.provider.calls == 1 and receipt.handle in gateway.store.windows


def test_metadata_shape_and_body_digest_are_strict() -> None:
    gateway = GatewayFixture(fixture_provider("local"))
    request = gateway.signed(body([USER])).request.model_dump(mode="json")
    for invalid in (
        {**request, "mode": "continue"},
        {**request, "handle": "ct1_" + "1" * 64},
        {**request, "contract": "other"},
        {**request, "expires_at": float("nan")},
    ):
        with pytest.raises(ValidationError):
            ContinuationRequest.model_validate(invalid)
    with pytest.raises(ValidationError):
        CallerScope.model_validate_json(
            '{"authority":"test","subject":"caller","generation":"1"}'
        )
    assert (
        InputBody.from_bytes(body([USER])) == gateway.signed(body([USER])).request.body
    )


def test_invalid_phase_cannot_dispatch_or_interrupt(gateway: GatewayFixture) -> None:
    first_turn(gateway)
    with pytest.raises(ContinuationFailure, match="invalid_transition"):
        dispatch(
            current(gateway),
            caller=gateway.callers["alice-token"],
            owner=gateway.owner,
            now=gateway.now,
        )
    with pytest.raises(ContinuationFailure, match="invalid_transition"):
        interrupt(current(gateway))


def test_missing_private_checkpoint_never_falls_back_to_fresh_provider_state(
    gateway: GatewayFixture,
) -> None:
    _, receipt = first_turn(gateway)
    raw = body([USER, ANSWER, FOLLOWUP])
    proof = gateway.signed(raw, receipt=receipt)
    state = gateway.admit(proof, raw)
    del gateway.store.windows[receipt.handle]
    with pytest.raises(ContinuationFailure, match="handle_unavailable"):
        gateway.send(proof, state)
    assert gateway.provider.calls == 1 and current(gateway).phase == "reserved"


def test_provider_switch_is_an_explicit_new_conversation(
    gateway: GatewayFixture,
) -> None:
    _, receipt = first_turn(gateway)
    raw = body([USER, ANSWER, FOLLOWUP])
    original = gateway.owner
    other = fixture_provider(
        "remote" if original.target.target_node.endswith("local") else "local"
    )
    gateway.owner = other.context()
    with pytest.raises(ContinuationFailure):
        gateway.admit(gateway.signed(raw, receipt=receipt), raw)
    assert gateway.provider.calls == 1 and other.calls == 0
    gateway.provider = other
    gateway.run(gateway.signed(raw, conversation="2" * 64), raw, ANSWER)
    assert other.received_windows == [None]


def test_client_chosen_principal_in_body_has_no_authentication_effect(
    gateway: GatewayFixture,
) -> None:
    raw = json.dumps(
        {"messages": [USER], "principal": "caller-b", "user": "caller-b"}
    ).encode()
    gateway.run(gateway.signed(raw), raw, ANSWER)
    assert current(gateway).binding.caller.subject == "caller-a"
    assert not any(key[1] == "caller-b" for key in gateway.store.states)


def test_start_collision_requires_explicit_new_conversation(
    gateway: GatewayFixture,
) -> None:
    first_turn(gateway)
    raw = body([USER])
    with pytest.raises(ContinuationFailure, match="conversation_exists"):
        gateway.admit(gateway.signed(raw), raw)
    assert gateway.provider.calls == 1


def test_restart_before_dispatch_can_release_only_the_spent_attempt(
    gateway: GatewayFixture,
) -> None:
    raw = body([USER])
    proof = gateway.signed(raw)
    gateway.admit(proof, raw)
    gateway.store = gateway.store.restarted()
    gateway.abort(proof)
    assert not gateway.store.states
    with pytest.raises(ContinuationFailure):
        gateway.admit(proof, raw)
    gateway.run(gateway.signed(raw), raw, ANSWER)
    assert gateway.provider.calls == 1
