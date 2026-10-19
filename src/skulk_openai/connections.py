"""Credential-free connection-state fixtures; no OAuth implementation is supplied."""

from typing import Literal, final

from pydantic import Field

from skulk_inference_reference.contracts import FrozenContract, Offer
from skulk_inference_reference.fixtures import fixture_offer


@final
class MockConnection(FrozenContract):
    """Synthetic consent/readiness state, deliberately incapable of holding tokens."""

    state: Literal["disconnected", "identity_only", "authorized", "unavailable"]
    enabled: bool = False
    revision: int = Field(default=1, ge=1)

    def offers(self) -> tuple[Offer, ...]:
        """Expose inference only with synthetic plan permission and enablement."""
        if not self.enabled or self.state != "authorized":
            return ()
        original = fixture_offer(external=True)
        return (
            Offer.model_validate(
                {**original.model_dump(), "offer_revision": self.revision}
            ),
        )
