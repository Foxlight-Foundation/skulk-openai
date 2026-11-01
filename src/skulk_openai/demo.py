"""Offline, credential-free routing demonstration; no Skulk node is launched."""

import asyncio
from contextlib import aclosing

from skulk_inference_reference.contracts import GenerationRequest, Message, Requirements
from skulk_inference_reference.fixtures import (
    fixture_authority,
    fixture_generate,
    fixture_offer,
)
from skulk_inference_reference.routing import select_offer, validate_dispatch

from .responses import MockResponsesAdapter, text_transport


async def demonstrate() -> None:
    """Discover two synthetic offers and route under two explicit policies."""
    request = GenerationRequest(messages=(Message(role="user", content="Hello"),))
    offers = (fixture_offer(), fixture_offer(external=True))
    authority = fixture_authority()
    for requirements in (
        Requirements(),
        Requirements(
            allowed_domains=frozenset({"local", "external"}),
            allowed_usage=frozenset({"none", "included_only"}),
            preferred_providers=("mock-openai",),
        ),
    ):
        selection = select_offer(offers, request, requirements, authority, now=1000.0)
        validate_dispatch(selection, selection.offer, request, authority, now=1000.0)
        print(
            f"Selected {selection.offer.provider}; rejected: "
            f"{[(r.offer_id, r.reasons) for r in selection.rejected]}"
        )
        # This dispatch table is the caller's plugin registry, not a vendor branch
        # in selection logic. A real fabric resolves the selected callable identity.
        streams = {
            "fixture": lambda: fixture_generate(request),
            "mock-openai": lambda model=selection.offer.model: MockResponsesAdapter(
                text_transport()
            ).generate(
                request,
                model=model,
            ),
        }
        async with aclosing(streams[selection.offer.provider]()) as stream:
            async for event in stream:
                if event.kind == "text":
                    print(event.text)
                else:
                    print(event.kind)


def main() -> None:
    """Run the offline demo without requesting provider or fleet access."""
    asyncio.run(demonstrate())


if __name__ == "__main__":
    main()
