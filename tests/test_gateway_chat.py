"""Delegated chat must preserve policy, account fencing, tools and terminal truth."""

import asyncio
import json
import secrets
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue, TypeAdapter

from skulk_inference_reference.chat import GatewayInvocation, GatewayProof
from skulk_openai.capability import InferenceCapability
from skulk_openai.chat_request import ChatRequest
from skulk_openai.chat_stream import chat_events
from skulk_openai.gateway import GatewayService
from skulk_openai.management import ManagementServer
from skulk_openai.responses import event_bytes
from skulk_openai.settings import IntegrationSettings, SettingsStore, account_binding
from tests.test_admission import invocation, prepared
from tests.test_management import browser


async def configured(tmp_path: Path):
    """Use an actual connected synthetic account and a separate gateway key."""
    capability, _, catalog, api, clock = await prepared(tmp_path)
    key = Ed25519PrivateKey.generate()
    service = GatewayService(capability, callable_id="fixture.chat", revision="a" * 16)
    settings = IntegrationSettings(
        inference_enabled=True,
        processing="allow_openai",
        usage="app_settings",
        models=("text-a", "text-b"),
        maximum_requests_per_hour=2,
        gateway_public_key=key.public_key().public_bytes_raw().hex(),
        account_binding=account_binding(catalog.registration_id, catalog.generation),
    )
    service.settings.save(settings)
    discovered = await service.catalog()
    assert discovered is not None
    invocation = GatewayInvocation(
        public_key=settings.gateway_public_key or "",
        target_node=discovered.target_node,
        callable_id=discovered.callable_id,
        descriptor_revision=discovered.descriptor_revision,
        catalog_revision=discovered.catalog_revision,
        model="text-a",
        request={"messages": [{"role": "user", "content": "hello"}]},
        nonce=secrets.token_hex(32),
        expires_at=clock() + 30,
    )
    proof = GatewayProof(
        invocation=invocation, signature=key.sign(invocation.canonical()).hex()
    )
    return capability, service, settings, key, proof, api, clock


def resign(
    proof: GatewayProof, key: Ed25519PrivateKey, **updates: object
) -> GatewayProof:
    """Sign a changed fixture request as the same independently delegated key."""
    invocation = GatewayInvocation.model_validate_json(
        proof.invocation.model_copy(update=updates).model_dump_json()
    )
    return GatewayProof(
        invocation=invocation, signature=key.sign(invocation.canonical()).hex()
    )


async def test_gateway_success_replay_and_shared_budget(tmp_path: Path) -> None:
    capability, service, _, key, proof, api, _ = await configured(tmp_path)
    events = [event async for event in service.generate(proof)]
    assert events[-1].kind == "completed" and events[0].delta == {"content": "hello"}
    starts = capability.provider.requests_started
    assert [e.error async for e in service.generate(proof)] == ["policy_denied"]
    second = resign(proof, key, nonce=secrets.token_hex(32))
    assert [e.kind async for e in service.generate(second)][-1] == "completed"
    third = resign(proof, key, nonce=secrets.token_hex(32))
    assert [e.error async for e in service.generate(third)] == ["policy_denied"]
    assert capability.provider.requests_started == starts + 1
    assert len(api.calls) == 3


@pytest.mark.parametrize(
    "change",
    [
        "key",
        "model",
        "target",
        "catalog",
        "expired",
        "tampered",
        "local_only",
        "sampling",
    ],
)
async def test_gateway_refuses_before_upstream(tmp_path: Path, change: str) -> None:
    capability, service, _, key, proof, _, clock = await configured(tmp_path)
    updates: dict[str, object] = {}
    if change == "key":
        key = Ed25519PrivateKey.generate()
        updates["public_key"] = key.public_key().public_bytes_raw().hex()
    elif change == "model":
        updates["model"] = "hidden-model"
    elif change == "target":
        updates["target_node"] = "other-host"
    elif change == "catalog":
        updates["catalog_revision"] = "b" * 64
    elif change == "expired":
        updates["expires_at"] = clock() - 1
    elif change == "local_only":
        updates["request"] = {
            **proof.invocation.request,
            "inference_policy": "local_only",
        }
    elif change == "sampling":
        updates["request"] = {**proof.invocation.request, "temperature": 0.7}
    else:
        updates["request"] = {"messages": [{"role": "user", "content": "tampered"}]}
    altered = resign(proof, key, **updates)
    if change == "tampered":
        altered = altered.model_copy(update={"signature": proof.signature})
    assert [e.kind async for e in service.generate(altered)] == ["failed"]
    assert capability.provider.requests_started == 0


async def test_concurrent_replay_has_one_admission_and_account_reconnect_fences_policy(
    tmp_path: Path,
) -> None:
    capability, service, _, _, proof, _, _ = await configured(tmp_path)

    async def consume() -> list[str]:
        return [e.kind async for e in service.generate(proof)]

    results = await asyncio.gather(consume(), consume())
    assert sum(result[-1] == "completed" for result in results) == 1
    assert capability.provider.requests_started == 1
    await capability.provider.sessions.disconnect()
    assert await service.catalog() is None
    assert [e.error async for e in service.generate(proof)] == ["policy_denied"]


async def test_settings_roundtrip_does_not_start_requests_or_expose_binding(
    tmp_path: Path,
) -> None:
    capability, service, settings, _, _, _, _ = await configured(tmp_path)
    async with browser(ManagementServer(capability)) as client:
        response = await client.post(
            "/api/settings",
            json=settings.model_dump(mode="json", exclude={"account_binding"}),
        )
        assert response.status_code == 200
        state = response.json()
        assert state["settings"]["models"] == ["text-a", "text-b"]
        assert "account_binding" not in state["settings"]
        assert capability.provider.requests_started == 0
        persisted = SettingsStore(capability.provider.sessions.store).read()
        assert persisted == settings
        assert (await client.get("/assets/skulk.svg")).status_code == 200
        assert (
            await client.post("/api/settings", json={"account_binding": "a" * 64})
        ).status_code == 400
        state["settings"]["processing"] = "local_only"
        assert (
            await client.post("/api/settings", json=state["settings"])
        ).status_code == 200
        assert await service.catalog() is None


def test_request_translation_preserves_history_tools_and_preview_restrictions() -> None:
    settings = IntegrationSettings(
        allow_function_tools=True, instructions="Be precise."
    )
    request = ChatRequest.model_validate_json(
        json.dumps(
            {
                "messages": [
                    {"role": "system", "content": "Write code"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "read", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
                    {"role": "user", "content": "continue"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "read",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "answer",
                        "schema": {"type": "object"},
                        "strict": True,
                    },
                },
            }
        )
    )
    body = request.responses(settings)
    assert body["store"] is False and body["stream"] is True
    assert "temperature" not in body and "max_output_tokens" not in body
    assert body["input"] == [
        {"role": "developer", "content": "Be precise."},
        {"role": "developer", "content": "Write code"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "read",
            "arguments": "{}",
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
        {"role": "user", "content": "continue"},
    ]
    with pytest.raises(ValueError):
        request.responses(IntegrationSettings())


async def test_chat_stream_preserves_tool_deltas_usage_and_late_usage_limit() -> None:
    raw = b"".join(
        event_bytes(TypeAdapter(dict[str, JsonValue]).validate_python(event))
        for event in [
            {"type": "response.reasoning_summary_text.delta", "delta": "Check it."},
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "item",
                    "call_id": "call",
                    "name": "read",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "item",
                "delta": "{}",
            },
            {
                "type": "response.completed",
                "response": {
                    "usage": {"input_tokens": 5, "output_tokens": 8, "total_tokens": 13}
                },
            },
        ]
    )
    events = [
        e
        async for e in chat_events(
            httpx.Response(200, content=raw), maximum_characters=256
        )
    ]
    assert events[0].delta == {"reasoning_content": "Check it."}
    assert events[-1].finish_reason == "tool_calls"
    assert events[-1].usage == {
        "prompt_tokens": 5,
        "completion_tokens": 8,
        "total_tokens": 13,
    }
    raw = event_bytes(
        {"type": "response.output_text.delta", "delta": "partial"}
    ) + event_bytes(
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "subscription_sharing_usage_limit_exceeded",
                    "message": "private body",
                }
            },
        }
    )
    failed = [
        e
        async for e in chat_events(
            httpx.Response(200, content=raw), maximum_characters=256
        )
    ]
    assert failed[-1].error == "usage_limit"
    assert "private body" not in failed[-1].model_dump_json()


async def test_expired_saved_connection_renews_through_explicit_catalog_refresh(
    tmp_path: Path,
) -> None:
    """Token expiry retains the account and permits normal renewal without consent."""
    capability, _, _, _, _, _, clock = await configured(tmp_path)
    account = capability.provider.sessions.store.load()
    assert account is not None and account.tokens is not None
    clock.now = account.tokens.expires_at + 1
    async with browser(ManagementServer(capability)) as client:
        state = (await client.get("/api/state")).json()
        assert state["connection"] == "expired"
        refreshed = await client.post("/api/models", json={})
        assert refreshed.status_code == 200
        assert refreshed.json()["connection"] == "authorized"
        latest = capability.provider.sessions.store.load()
        assert latest is not None and latest.generation == account.generation
        assert capability.provider.requests_started == 0


async def test_model_features_require_both_provider_declaration_and_owner_permission(
    tmp_path: Path,
) -> None:
    """Unknown model metadata must not create image or function-tool UI controls."""
    capability, service, settings, _, _, _, _ = await configured(tmp_path)
    snapshot = capability.cache.read()
    assert snapshot is not None
    declared = snapshot.models[0].model_copy(
        update={
            "input_modalities": ("text", "image"),
            "supports_parallel_tool_calls": True,
        }
    )
    capability.cache.save(
        snapshot.model_copy(update={"models": (declared, *snapshot.models[1:])})
    )
    catalog = await service.catalog()
    assert catalog is not None and all(not model.features for model in catalog.models)
    service.settings.save(
        settings.model_copy(update={"allow_images": True, "allow_function_tools": True})
    )
    catalog = await service.catalog()
    assert catalog is not None
    assert catalog.models[0].features == ("image_input", "function_tools")
    assert catalog.models[1].features == ()


@pytest.mark.parametrize("saved_enabled", [False, True])
@pytest.mark.parametrize("startup_enabled", [False, True])
async def test_saved_enablement_overrides_startup_after_restart(
    tmp_path: Path, saved_enabled: bool, startup_enabled: bool
) -> None:
    """A saved disable survives both UI and managed startup enable flags."""
    original, credential, catalog, _, clock = await prepared(tmp_path)
    original.settings.save(IntegrationSettings(inference_enabled=saved_enabled))
    restarted = InferenceCapability(
        original.provider,
        target_node=original.target_node,
        callable_id=original.callable_id,
        descriptor_revision=original.descriptor_revision,
        enabled=startup_enabled,
        clock=clock,
    )
    assert restarted.enabled is saved_enabled
    assert bool((await restarted.discovery()).offers) is saved_enabled
    signed = credential.sign(invocation(catalog, clock))
    events = [event async for event in restarted.generate(signed)]
    assert events[-1].kind == ("completed" if saved_enabled else "failed")
    assert restarted.provider.requests_started == int(saved_enabled)
