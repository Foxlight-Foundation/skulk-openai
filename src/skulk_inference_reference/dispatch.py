"""Reference provider-side admission before any injected generation side effect."""

from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing

from .contracts import (
    Authority,
    GenerationEvent,
    GenerationRequest,
    Offer,
    Requirements,
)
from .routing import rejection_reasons


def admitted_stream(
    generate: Callable[[GenerationRequest], AsyncGenerator[GenerationEvent]],
    request: GenerationRequest,
    requirements: Requirements,
    current_offer: Callable[[], Offer],
    current_authority: Callable[[], Authority],
    *,
    now: Callable[[], float],
) -> AsyncGenerator[GenerationEvent]:
    """Recheck trusted local snapshots on iteration, including direct invocation.

    Snapshot suppliers belong to the trusted synthetic owner, never request JSON.
    Production distributed caller authentication and revocation are not implemented.
    """

    async def stream() -> AsyncGenerator[GenerationEvent]:
        if rejection_reasons(
            current_offer(), request, requirements, current_authority(), now()
        ):
            yield GenerationEvent(kind="failed", error="policy_denied")
            return
        async with aclosing(generate(request)) as output:
            async for event in output:
                yield event

    return stream()
