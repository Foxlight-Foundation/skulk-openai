"""A second independently identified provider and synthetic routing fixtures."""

import asyncio
from collections.abc import AsyncGenerator

from .contracts import Authority, GenerationEvent, GenerationRequest, Offer


def fixture_offer(*, external: bool = False) -> Offer:
    """Return one explicitly synthetic offer under the shared draft contract."""
    provider = "mock-openai" if external else "fixture"
    return Offer(
        offer_id=f"{provider}:text",
        provider=provider,
        model="synthetic-text",
        callable_id=f"{provider}.inference.generate",
        descriptor_revision="a" * 16,
        offer_revision=1,
        domain="external" if external else "local",
        usage="included_only" if external else "none",
        context_tokens=8192,
        ready=True,
        expires_at=2000.0,
    )


def fixture_authority() -> Authority:
    """Create a synthetic owner grant; it is not a real identity or OAuth token."""
    return Authority(
        principal="demo-operator",
        revision=1,
        allowed_offers=frozenset({"mock-openai:text", "fixture:text"}),
        allowed_domains=frozenset({"local", "external"}),
        allowed_usage=frozenset({"none", "included_only"}),
        maximum_output_characters=4096,
        expires_at=2000.0,
    )


async def fixture_generate(
    request: GenerationRequest,
) -> AsyncGenerator[GenerationEvent]:
    """Yield independent text with an await point for cancellation qualification."""
    text = "Hello from the independent fixture provider."
    if len(text) > request.maximum_output_characters:
        yield GenerationEvent(kind="failed", error="output_limit")
        return
    await asyncio.sleep(0)
    yield GenerationEvent(kind="text", text=text)
    yield GenerationEvent(kind="completed")
