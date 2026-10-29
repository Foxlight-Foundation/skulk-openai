"""Deterministic hard filtering and dispatch revalidation, without vendor branches."""

import hashlib
import math
from collections.abc import Sequence

from .contracts import (
    CONTRACT,
    Authority,
    GenerationRequest,
    Offer,
    Rejection,
    RejectionReason,
    Requirements,
    Selection,
)


class NoEligibleOffer(Exception):
    """No offer satisfied the supplied hard constraints and synthetic authority."""

    def __init__(self, rejected: tuple[Rejection, ...]) -> None:
        """Retain content-free rejection facts for inspection."""
        super().__init__("no eligible inference offer")
        self.rejected = rejected


class DispatchDenied(Exception):
    """The selected target, request or trusted authority changed before dispatch."""


def request_digest(request: GenerationRequest) -> str:
    """Bind a selection to its exact input without retaining input in explanations."""
    return hashlib.sha256(request.model_dump_json().encode()).hexdigest()


def rejection_reasons(
    offer: Offer,
    request: GenerationRequest,
    requirements: Requirements,
    authority: Authority,
    now: float,
) -> tuple[RejectionReason, ...]:
    """Return all hard denials; missing context evidence never satisfies a bound."""
    if not math.isfinite(now) or now < 0:
        raise ValueError("clock must be finite and nonnegative")
    reasons: list[RejectionReason] = []
    if offer.contract != CONTRACT:
        reasons.append("contract_mismatch")
    if not offer.ready:
        reasons.append("not_ready")
    if offer.expires_at <= now:
        reasons.append("expired_offer")
    if authority.expires_at <= now:
        reasons.append("expired_authority")
    if offer.offer_id not in authority.allowed_offers:
        reasons.append("offer_forbidden")
    if (
        offer.domain not in requirements.allowed_domains
        or offer.domain not in authority.allowed_domains
    ):
        reasons.append("domain_forbidden")
    if (
        offer.usage not in requirements.allowed_usage
        or offer.usage not in authority.allowed_usage
    ):
        reasons.append("usage_forbidden")
    if requirements.minimum_context_tokens is not None:
        if offer.context_tokens is None:
            reasons.append("context_unknown")
        elif offer.context_tokens < requirements.minimum_context_tokens:
            reasons.append("context_insufficient")
    if request.maximum_output_characters > authority.maximum_output_characters:
        reasons.append("output_forbidden")
    return tuple(reasons)


def select_offer(
    offers: Sequence[Offer],
    request: GenerationRequest,
    requirements: Requirements,
    authority: Authority,
    *,
    now: float,
) -> Selection:
    """Filter first, rank eligible offers by preference then stable identity.

    Duplicate identities are refused instead of allowing input order to decide
    which model or permission facts an identity means.
    """
    if len(offers) > 128 or len({offer.offer_id for offer in offers}) != len(offers):
        raise ValueError("offers must be bounded and uniquely identified")
    rejected: list[Rejection] = []
    eligible: list[Offer] = []
    for offer in sorted(offers, key=lambda item: item.offer_id):
        reasons = rejection_reasons(offer, request, requirements, authority, now)
        if reasons:
            rejected.append(Rejection(offer_id=offer.offer_id, reasons=reasons))
        else:
            eligible.append(offer)
    if not eligible:
        raise NoEligibleOffer(tuple(rejected))

    def preference(offer: Offer) -> tuple[int, str]:
        try:
            rank = requirements.preferred_providers.index(offer.provider)
        except ValueError:
            rank = len(requirements.preferred_providers)
        return rank, offer.offer_id

    return Selection(
        offer=min(eligible, key=preference),
        principal=authority.principal,
        authority_revision=authority.revision,
        request_digest=request_digest(request),
        requirements=requirements,
        rejected=tuple(rejected),
    )


def validate_dispatch(
    selection: Selection,
    current_offer: Offer,
    request: GenerationRequest,
    authority: Authority,
    *,
    now: float,
) -> None:
    """Reject stale snapshots or any current denial before provider side effects.

    The trusted owner must serialize admission with revocation. This pure check
    is a local reference, not a distributed authorization implementation.
    """
    if (
        selection.offer != current_offer
        or selection.principal != authority.principal
        or selection.authority_revision != authority.revision
        or selection.request_digest != request_digest(request)
        or rejection_reasons(
            current_offer, request, selection.requirements, authority, now
        )
    ):
        raise DispatchDenied("inference dispatch rejected by current policy")
