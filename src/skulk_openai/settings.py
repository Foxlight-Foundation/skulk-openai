"""Durable owner policy; stores no credential, prompt, or response content."""

import hashlib
from typing import Literal

from pydantic import Field, model_validator

from skulk_inference_reference.contracts import FrozenContract, Identifier

from .credentials import CredentialStore


class IntegrationSettings(FrozenContract):
    """Editable integration controls, enforced independently of the gateway."""

    inference_enabled: bool = False
    processing: Literal["local_only", "allow_openai"] = "local_only"
    usage: Literal["included_only", "app_settings"] = "included_only"
    models: tuple[Identifier, ...] = Field(default=(), max_length=128)
    default_model: Identifier | None = None
    instructions: str = Field(default="", max_length=4096)
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
        | None
    ) = None
    verbosity: Literal["low", "medium", "high"] | None = None
    allow_function_tools: bool = False
    allow_images: bool = False
    maximum_output_characters: int = Field(default=32768, ge=256, le=131072)
    maximum_requests_per_hour: int = Field(default=60, ge=1, le=1000)
    gateway_public_key: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    # Bind persistent permission to the selected account generation. These
    # owner-only fields never appear in public discovery or browser state.
    account_binding: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    plan_notice_seen: bool = False

    @model_validator(mode="after")
    def choices(self) -> "IntegrationSettings":
        """Reject duplicate models and a default outside the explicit allowlist."""
        if len(set(self.models)) != len(self.models) or (
            self.default_model is not None and self.default_model not in self.models
        ):
            raise ValueError("invalid model selection")
        return self


def account_binding(registration_id: str, generation: int) -> str:
    """Fence a delegation after account switch, disconnect, or reconnection."""
    return hashlib.sha256(f"{registration_id}:{generation}".encode()).hexdigest()


class SettingsStore:
    """Use the protected account journal for atomic owner-policy replacement."""

    def __init__(self, store: CredentialStore) -> None:
        """Retain the capability-owned transactional store."""
        self.store = store

    def read(self) -> IntegrationSettings:
        """Return fail-closed defaults until the owner saves integration policy."""
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='integration_settings'"
            ).fetchone()
        return (
            IntegrationSettings()
            if row is None
            else IntegrationSettings.model_validate_json(str(row[0]))
        )

    def configured(self) -> bool:
        """Distinguish migrated explicit policy from the original test-only UI."""
        with self.store.transaction() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM metadata WHERE key='integration_settings'"
                ).fetchone()
                is not None
            )

    def save(self, settings: IntegrationSettings) -> None:
        """Replace policy atomically; no network operation or inference occurs."""
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('integration_settings', ?)",
                (settings.model_dump_json(),),
            )
