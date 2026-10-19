"""Observable selection and provider-side denial tests for the draft contract."""

from itertools import permutations
from typing import Literal

import httpx
import pytest
from pydantic import ValidationError

from skulk_inference_reference.contracts import (
    Authority,
    GenerationRequest,
    Message,
    Offer,
    Requirements,
)
from skulk_inference_reference.dispatch import admitted_stream
from skulk_inference_reference.fixtures import fixture_authority, fixture_offer
from skulk_inference_reference.routing import (
    DispatchDenied,
    NoEligibleOffer,
    select_offer,
    validate_dispatch,
)
from skulk_openai.connections import MockConnection
from skulk_openai.responses import MockResponsesAdapter, event_bytes


def request() -> GenerationRequest:
    """Return bounded synthetic input, never a credential or private prompt."""
    return GenerationRequest(messages=(Message(role="user", content="hello"),))


def external_requirements() -> Requirements:
    """Permit both synthetic domains while preferring the external fixture."""
    return Requirements(
        allowed_domains=frozenset({"local", "external"}),
        allowed_usage=frozenset({"none", "included_only"}),
        preferred_providers=("mock-openai",),
    )


def test_hard_filter_precedes_preference() -> None:
    requirements = Requirements(preferred_providers=("mock-openai",))
    choice = select_offer(
        (fixture_offer(external=True), fixture_offer()),
        request(),
        requirements,
        fixture_authority(),
        now=1000.0,
    )
    assert choice.offer.provider == "fixture"
    assert choice.rejected[0].reasons == ("domain_forbidden", "usage_forbidden")


def test_second_provider_and_withdrawal_require_no_selector_change() -> None:
    requirements = external_requirements()
    for offers in permutations((fixture_offer(), fixture_offer(external=True))):
        choice = select_offer(
            offers, request(), requirements, fixture_authority(), now=1000.0
        )
        assert choice.offer.provider == "mock-openai"
    remaining = select_offer(
        (fixture_offer(),), request(), requirements, fixture_authority(), now=1000.0
    )
    assert remaining.offer.provider == "fixture"
    assert fixture_offer().callable_id != fixture_offer(external=True).callable_id


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("contract", "text.generate@99.0.0", "contract_mismatch"),
        ("ready", False, "not_ready"),
        ("expires_at", 1000.0, "expired_offer"),
        ("context_tokens", None, "context_unknown"),
        ("context_tokens", 1, "context_insufficient"),
        ("usage", "unknown", "usage_forbidden"),
        ("usage", "credits_possible", "usage_forbidden"),
    ],
)
def test_hard_requirement_failure(field: str, value: object, reason: str) -> None:
    offer = Offer.model_validate({**fixture_offer().model_dump(), field: value})
    with pytest.raises(NoEligibleOffer) as failure:
        select_offer(
            (offer,),
            request(),
            Requirements(minimum_context_tokens=2),
            fixture_authority(),
            now=1000.0,
        )
    assert reason in failure.value.rejected[0].reasons


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", 2),
        ("principal", "another-operator"),
        ("allowed_offers", frozenset[str]()),
        ("expires_at", 999.0),
    ],
)
def test_dispatch_rechecks_grant(field: str, value: object) -> None:
    grant = fixture_authority()
    choice = select_offer(
        (fixture_offer(),), request(), Requirements(), grant, now=1000.0
    )
    current = Authority.model_validate({**grant.model_dump(), field: value})
    with pytest.raises(DispatchDenied):
        validate_dispatch(choice, fixture_offer(), request(), current, now=1000.0)


def test_dispatch_binds_exact_offer_and_input() -> None:
    grant = fixture_authority()
    choice = select_offer(
        (fixture_offer(),), request(), Requirements(), grant, now=1000.0
    )
    changed = Offer.model_validate(
        {**fixture_offer().model_dump(), "offer_revision": 2}
    )
    with pytest.raises(DispatchDenied):
        validate_dispatch(choice, changed, request(), grant, now=1000.0)
    with pytest.raises(DispatchDenied):
        validate_dispatch(
            choice,
            fixture_offer(),
            GenerationRequest(messages=(Message(role="user", content="different"),)),
            grant,
            now=1000.0,
        )


@pytest.mark.parametrize("now", [float("nan"), float("inf"), -1.0])
def test_invalid_clock_cannot_make_an_expired_offer_eligible(now: float) -> None:
    with pytest.raises(ValueError):
        select_offer(
            (fixture_offer(),), request(), Requirements(), fixture_authority(), now=now
        )


def test_duplicate_offer_identity_is_not_order_dependent() -> None:
    with pytest.raises(ValueError):
        select_offer(
            (fixture_offer(), fixture_offer()),
            request(),
            Requirements(),
            fixture_authority(),
            now=1000.0,
        )


@pytest.mark.parametrize("mode", ["direct", "revoked_after_selection"])
async def test_denied_content_never_reaches_http_sink(mode: str) -> None:
    calls: list[httpx.Request] = []

    def sink(incoming: httpx.Request) -> httpx.Response:
        calls.append(incoming)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=event_bytes({"type": "response.completed"}),
        )

    adapter = MockResponsesAdapter(httpx.MockTransport(sink))
    grant = fixture_authority()
    offer = fixture_offer(external=True)
    requirements = Requirements() if mode == "direct" else external_requirements()
    stream = admitted_stream(
        lambda item: adapter.generate(item, model=offer.model),
        request(),
        requirements,
        lambda: offer,
        lambda: grant,
        now=lambda: 1000.0,
    )
    if mode == "revoked_after_selection":
        grant = Authority.model_validate(
            {**grant.model_dump(), "allowed_offers": frozenset()}
        )
    events = [event async for event in stream]
    assert [event.error for event in events] == ["policy_denied"]
    assert calls == []


@pytest.mark.parametrize("state", ["disconnected", "identity_only", "unavailable"])
def test_connection_is_not_inference_permission(
    state: Literal["disconnected", "identity_only", "unavailable"],
) -> None:
    assert MockConnection(state=state, enabled=True).offers() == ()


def test_authorized_connection_still_requires_enablement() -> None:
    assert MockConnection(state="authorized", enabled=False).offers() == ()
    offers = MockConnection(state="authorized", enabled=True, revision=7).offers()
    assert len(offers) == 1 and offers[0].offer_revision == 7


def test_authority_cannot_be_smuggled_into_request() -> None:
    with pytest.raises(ValidationError):
        GenerationRequest.model_validate(
            {
                "messages": (Message(role="user", content="hello"),),
                "authority": fixture_authority().model_dump(),
            }
        )


def test_request_encoding_and_types_are_bounded() -> None:
    with pytest.raises(ValidationError):
        GenerationRequest(
            messages=tuple(Message(role="user", content="x" * 4096) for _ in range(16))
        )
    with pytest.raises(ValidationError):
        GenerationRequest.model_validate(
            {"messages": (), "maximum_output_characters": True}
        )


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("allowed_domains", frozenset({"local"}), "domain_forbidden"),
        ("allowed_usage", frozenset({"none"}), "usage_forbidden"),
        ("maximum_output_characters", 1, "output_forbidden"),
    ],
)
def test_caller_cannot_weaken_host_authority(
    field: str,
    value: object,
    reason: str,
) -> None:
    grant = Authority.model_validate({**fixture_authority().model_dump(), field: value})
    with pytest.raises(NoEligibleOffer) as failure:
        select_offer(
            (fixture_offer(external=True),),
            request(),
            external_requirements(),
            grant,
            now=1000.0,
        )
    assert reason in failure.value.rejected[0].reasons


def test_astral_unicode_bound_matches_managed_control_encoding() -> None:
    with pytest.raises(ValidationError, match="byte bound"):
        GenerationRequest(
            messages=tuple(Message(role="user", content="🦊" * 3000) for _ in range(2))
        )
