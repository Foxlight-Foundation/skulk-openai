"""Guarded continuation adapter; provider state remains in capability custody."""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Self

from pydantic import JsonValue, model_validator

from skulk_inference_reference.chat import ChatEvent
from skulk_inference_reference.continuation import (
    ContinuationFailure,
    ContinuationReceipt,
    FailureCode,
)
from skulk_inference_reference.continuation_auth import ContinuationProof
from skulk_inference_reference.contracts import FrozenContract

from .auth_contracts import AuthFailure
from .canonical import (
    AssistantAssembler,
    CanonicalWindow,
    CompactionProfile,
    ResponseCapture,
)
from .chat_request import ChatRequest
from .checkpoints import CheckpointLease, CheckpointWorker
from .file_input import document_features, validate_document_features
from .provider import CatalogCache, ProviderClient, ProviderFailure
from .settings import SettingsStore


class ContinuationEvent(FrozenContract):
    """Public chat event plus a committed receipt or bounded recovery classification."""

    event: ChatEvent
    receipt: ContinuationReceipt | None = None
    continuation_error: FailureCode | None = None

    @model_validator(mode="after")
    def terminal_shape(self) -> Self:
        """Never present uncommitted provider output as a completed continuation."""
        if (self.event.kind == "completed") != (self.receipt is not None):
            raise ValueError("completion requires committed continuation receipt")
        if self.continuation_error is not None and self.event.kind != "failed":
            raise ValueError("continuation error requires a failed event")
        return self


class ContinuationService:
    """Translate authenticated new history over a protected canonical window.

    This source adapter is not registered in an installed SDK descriptor or HTTP
    gateway. Its owner supplies an exclusively fenced worker and an explicitly
    established model profile; omitting the profile leaves automatic compaction
    disabled. The caller always supplies full public history for compatibility.
    """

    def __init__(
        self,
        worker: CheckpointWorker,
        provider: ProviderClient,
        *,
        profile: CompactionProfile | None = None,
    ) -> None:
        """Bind capability-owned effects; constructing the adapter performs no I/O."""
        self.worker = worker
        self.provider = provider
        self.profile = profile

    async def generate(
        self, proof: ContinuationProof, raw: bytes
    ) -> AsyncGenerator[ContinuationEvent]:
        """Authenticate before egress and commit only verified terminal state."""
        lease: CheckpointLease | None = None
        capture: ResponseCapture | None = None
        try:
            lease = await self._begin(proof, raw)
            settings = await asyncio.to_thread(
                SettingsStore(self.worker.store.credentials).read
            )
            request = ChatRequest.model_validate_json(raw)
            body = request.responses(
                settings, skip_messages=lease.record.metadata.state.history.count
            )
            catalog = await asyncio.to_thread(CatalogCache(self.provider.sessions).read)
            if catalog is None:
                raise ContinuationFailure("authority_unavailable")
            model = next(
                (
                    item
                    for item in catalog.models
                    if item.slug == proof.request.target.model
                ),
                None,
            )
            if model is None:
                raise ContinuationFailure("authority_unavailable")
            validate_document_features(
                body,
                document_features(
                    model.input_modalities, allow_images=settings.allow_images
                ),
            )
            revision = self.profile.revision if self.profile else None
            prior: tuple[dict[str, JsonValue], ...] = ()
            if lease.record.window:
                window = CanonicalWindow.decode(lease.record.window)
                if window.profile_revision != revision:
                    raise ContinuationFailure("scope_mismatch")
                prior = window.items
            new_items = body.get("input")
            if not isinstance(new_items, list) or any(
                not isinstance(item, dict) for item in new_items
            ):
                raise ValueError("invalid translated input")
            items: tuple[dict[str, JsonValue], ...] = (
                *prior,
                *(item for item in new_items if isinstance(item, dict)),
            )
            # Apply the same custody bound before provider submission. Public
            # history may be much larger than a compacted working window.
            CanonicalWindow(items=items, profile_revision=revision).encode()
            capture = ResponseCapture(
                items,
                profile_revision=revision,
                allow_empty_terminal=(
                    self.profile is not None
                    and self.profile.terminal_output == "empty_snapshot"
                ),
            )
            upstream: dict[str, object] = {
                **body,
                "input": list(items),
                "model": model.slug,
            }
            if self.profile is not None:
                if self.profile.model != model.slug:
                    raise ContinuationFailure("scope_mismatch")
                upstream["context_management"] = [
                    {
                        "type": "compaction",
                        "compact_threshold": self.profile.compact_threshold,
                    }
                ]
            await asyncio.to_thread(lease.dispatch)
            tools = body.get("tools")
            names: list[str] = []
            if isinstance(tools, list):
                for tool in tools:
                    name = tool.get("name") if isinstance(tool, dict) else None
                    if isinstance(name, str):
                        names.append(name)
            allowed: frozenset[str] = frozenset(names)
            choice = body.get("tool_choice")
            if choice == "none":
                allowed = frozenset()
            elif isinstance(choice, dict):
                selected = choice.get("name")
                if isinstance(selected, str):
                    allowed = allowed.intersection({selected})
            assistant = AssistantAssembler(
                allowed_functions=allowed,
                allow_parallel=body.get("parallel_tool_calls") is not False,
            )
            async with aclosing(
                self.provider.chat(
                    upstream,
                    catalog=catalog,
                    maximum_characters=settings.maximum_output_characters,
                    capture=capture,
                )
            ) as events:
                async for event in events:
                    if event.kind == "delta":
                        assistant.append(event.delta)
                        yield ContinuationEvent(event=event)
                    elif event.kind == "completed":
                        if capture.window is None:
                            raise ValueError("missing complete canonical window")
                        message = assistant.message()
                        capture.validate_message(message)
                        receipt = await asyncio.to_thread(
                            lease.commit, message, capture.window
                        )
                        yield ContinuationEvent(event=event, receipt=receipt)
                    else:
                        await asyncio.to_thread(lease.close)
                        yield ContinuationEvent(event=event)
        except ContinuationFailure as failure:
            await self._retire(lease)
            yield ContinuationEvent(
                event=ChatEvent(kind="failed", error="policy_denied"),
                continuation_error=failure.code,
            )
        except ValueError:
            await self._retire(lease)
            yield ContinuationEvent(
                event=ChatEvent(kind="failed", error="invalid_request")
            )
        except (AuthFailure, ProviderFailure, OSError, sqlite3.Error):
            await self._retire(lease)
            yield ContinuationEvent(
                event=ChatEvent(kind="failed", error="policy_denied"),
                continuation_error="authority_unavailable",
            )
        finally:
            if capture is not None:
                capture.discard()
            await self._retire(lease)

    async def _retire(self, lease: CheckpointLease | None) -> None:
        if lease is not None:
            try:
                await asyncio.to_thread(lease.close)
            except ContinuationFailure:
                # Dispatch uncertainty was persisted before upstream I/O. If
                # storage is unavailable during cleanup, restart recovery
                # fences the abandoned reservation; never synthesize a receipt.
                pass

    async def _begin(self, proof: ContinuationProof, raw: bytes) -> CheckpointLease:
        # A cancelled to_thread await does not cancel its SQL transaction. Own
        # the eventual lease before propagating cancellation, or a reservation
        # could outlive the only coroutine able to retire it.
        pending = asyncio.create_task(asyncio.to_thread(self.worker.begin, proof, raw))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            while True:
                try:
                    lease = await asyncio.shield(pending)
                except asyncio.CancelledError:
                    # Repeated cancellation still cannot detach a transaction
                    # from its eventual lease; SQL itself has a bounded timeout.
                    continue
                except ContinuationFailure:
                    break
                else:
                    await self._retire(lease)
                    break
            raise
