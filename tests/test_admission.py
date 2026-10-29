"""Real signing keys and the capability boundary guard a synthetic outbound sink."""

import asyncio
import secrets
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest

from skulk_inference_reference.contracts import Requirements
from skulk_openai.admission import CallerCredential, GrantStore, Invocation
from skulk_openai.auth_contracts import AuthFailure
from skulk_openai.caller import read_credential, write_credential
from skulk_openai.capability import InferenceCapability
from skulk_openai.credentials import CredentialStore
from skulk_openai.provider import CatalogCache, CatalogSnapshot, ProviderClient
from tests.test_provider import API, collect, request
from tests.test_sessions import Clock, connect, setup


async def prepared(
    tmp_path: Path, *, maximum_attempts: int = 2
) -> tuple[InferenceCapability, CallerCredential, CatalogSnapshot, API, Clock]:
    """Prepare only generated accounts, a protected journal and a mocked HTTP sink."""
    manager, issuer, clock = setup(tmp_path)
    await connect(manager, issuer)
    api = API()
    provider = ProviderClient(
        manager, transport=httpx.MockTransport(api.handle), clock=clock
    )
    catalog = await provider.catalog()
    CatalogCache(manager).save(catalog)
    capability = InferenceCapability(
        provider,
        target_node="managed.reference",
        callable_id="openai.inference.generate",
        descriptor_revision="a" * 16,
        enabled=True,
        clock=clock,
    )
    credential = capability.grants.issue(
        catalog,
        models=("text-a",),
        maximum_attempts=maximum_attempts,
        maximum_output_characters=1024,
        lifetime_seconds=300,
        allow_external_unknown_usage=True,
    )
    return capability, credential, catalog, api, clock


def invocation(catalog: CatalogSnapshot, clock: Clock) -> Invocation:
    """Explicitly allow external/unknown usage without asserting a context bound."""
    return Invocation(
        target_node="managed.reference",
        callable_id="openai.inference.generate",
        descriptor_revision="a" * 16,
        catalog_revision=catalog.revision,
        model="text-a",
        request=request(),
        requirements=Requirements(
            allowed_domains=frozenset({"external"}),
            allowed_usage=frozenset({"unknown"}),
            minimum_context_tokens=None,
        ),
        nonce=secrets.token_hex(32),
        expires_at=clock() + 30,
    )


async def test_independent_capability_boundary_accepts_signed_once_only(
    tmp_path: Path,
) -> None:
    capability, credential, catalog, api, clock = await prepared(tmp_path)
    signed = credential.sign(invocation(catalog, clock))
    assert (await collect(capability.generate(signed)))[-1].kind == "completed"
    assert (await collect(capability.generate(signed)))[-1].error == "policy_denied"
    assert len(api.calls) == 2
    assert credential.private_key.get_secret_value() not in repr(credential)
    assert b"private input" not in capability.grants.credentials.path.read_bytes()


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_node", "other.target"),
        ("callable_id", "other.generate"),
        ("descriptor_revision", "b" * 16),
        ("catalog_revision", "b" * 64),
        ("model", "text-b"),
    ],
)
async def test_valid_signer_cannot_widen_owner_grant_or_target(
    tmp_path: Path, field: str, value: str
) -> None:
    capability, credential, catalog, api, clock = await prepared(tmp_path)
    changed = invocation(catalog, clock).model_copy(update={field: value})
    result = await collect(capability.generate(credential.sign(changed)))
    assert result[-1].error == "policy_denied" and len(api.calls) == 1


@pytest.mark.parametrize("denial", ["signature", "tamper", "expired", "future"])
async def test_invalid_or_expired_proof_keeps_content_local(
    tmp_path: Path, denial: str
) -> None:
    capability, credential, catalog, api, clock = await prepared(tmp_path)
    original = invocation(catalog, clock)
    signed = credential.sign(original)
    if denial == "signature":
        signed = signed.model_copy(update={"signature": "0" * 128})
    elif denial == "tamper":
        signed = signed.model_copy(
            update={"invocation": original.model_copy(update={"model": "text-b"})}
        )
    else:
        when = clock() - 1 if denial == "expired" else clock() + 61
        signed = credential.sign(original.model_copy(update={"expires_at": when}))
    result = await collect(capability.generate(signed))
    assert result[-1].error == "policy_denied" and len(api.calls) == 1


@pytest.mark.parametrize("denial", ["local-only", "included-only", "context", "output"])
async def test_hard_request_policy_is_enforced_even_for_valid_signers(
    tmp_path: Path, denial: str
) -> None:
    capability, credential, catalog, api, clock = await prepared(tmp_path)
    original = invocation(catalog, clock)
    policy = original.requirements
    if denial == "local-only":
        policy = policy.model_copy(update={"allowed_domains": frozenset({"local"})})
    elif denial == "included-only":
        policy = policy.model_copy(
            update={"allowed_usage": frozenset({"included_only"})}
        )
    elif denial == "context":
        policy = policy.model_copy(update={"minimum_context_tokens": 1})
    else:
        original = original.model_copy(
            update={
                "request": original.request.model_copy(
                    update={"maximum_output_characters": 1025}
                )
            }
        )
    signed = credential.sign(original.model_copy(update={"requirements": policy}))
    assert (await collect(capability.generate(signed)))[-1].error == "policy_denied"
    assert len(api.calls) == 1


async def test_finite_attempt_budget_is_not_refunded_after_failure(
    tmp_path: Path,
) -> None:
    capability, credential, catalog, api, clock = await prepared(
        tmp_path, maximum_attempts=1
    )
    api.status = 500
    result = await collect(
        capability.generate(credential.sign(invocation(catalog, clock)))
    )
    assert result[-1].error == "upstream_failed"
    api.status = 200
    result = await collect(
        capability.generate(credential.sign(invocation(catalog, clock)))
    )
    assert result[-1].error == "policy_denied" and len(api.calls) == 2


@pytest.mark.parametrize("reuse_nonce", [True, False])
async def test_concurrent_replays_and_budget_races_admit_only_once(
    tmp_path: Path,
    reuse_nonce: bool,
) -> None:
    capability, credential, catalog, api, clock = await prepared(
        tmp_path, maximum_attempts=1
    )
    signed = credential.sign(invocation(catalog, clock))
    requests = [
        signed if reuse_nonce else credential.sign(invocation(catalog, clock))
        for _ in range(4)
    ]
    results = await asyncio.gather(
        *(collect(capability.generate(item)) for item in requests)
    )
    assert sum(items[-1].kind == "completed" for items in results) == 1
    assert len(api.calls) == 2


async def test_restart_keeps_replay_fence_and_revocation(tmp_path: Path) -> None:
    capability, credential, catalog, api, clock = await prepared(tmp_path)
    signed = credential.sign(invocation(catalog, clock))
    assert (await collect(capability.generate(signed)))[-1].kind == "completed"
    restarted = GrantStore(
        CredentialStore(capability.grants.credentials.root), clock=clock
    )
    capability.grants = restarted
    assert (await collect(capability.generate(signed)))[-1].error == "policy_denied"
    restarted.revoke(credential.grant.grant_id)
    signed = credential.sign(invocation(catalog, clock))
    assert (await collect(capability.generate(signed)))[-1].error == "policy_denied"
    assert len(api.calls) == 2


@pytest.mark.parametrize("denial", ["disconnect", "reauthorize", "disabled", "stale"])
async def test_account_or_offer_withdrawal_blocks_previously_signed_calls(
    tmp_path: Path, denial: str
) -> None:
    capability, credential, catalog, api, clock = await prepared(tmp_path)
    signed = credential.sign(invocation(catalog, clock))
    if denial == "disconnect":
        await capability.provider.sessions.disconnect()
    elif denial == "reauthorize":
        store = capability.grants.credentials
        saved = store.load()
        assert saved is not None and saved.tokens is not None
        store.connect(
            saved.subject,
            saved.client_id,
            saved.tokens,
            expected_generation=saved.generation,
            expected_epoch=store.epoch(),
        )
    elif denial == "disabled":
        capability.enabled = False
    else:
        clock.now += 61
    assert await capability.offers() == ()
    assert (await collect(capability.generate(signed)))[-1].error == "policy_denied"
    assert len(api.calls) == 1


async def test_discovery_does_not_refresh_or_call_upstream(tmp_path: Path) -> None:
    capability, _, _, api, _ = await prepared(tmp_path)
    first = await capability.offers()
    second = await capability.offers()
    assert first == second and len(first) == 2
    assert all(
        offer.domain == "external" and offer.usage == "unknown" for offer in first
    )
    assert all(offer.context_tokens is None for offer in first)
    assert len(api.calls) == 1


async def test_exported_caller_key_is_private_and_cannot_overwrite_files(
    tmp_path: Path,
) -> None:
    capability, credential, catalog, api, clock = await prepared(tmp_path)
    path = write_credential(capability.grants.credentials, credential)
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = read_credential(path)
    assert loaded == credential
    with pytest.raises(FileExistsError):
        write_credential(capability.grants.credentials, credential)
    signed = loaded.sign(invocation(catalog, clock))
    assert (await collect(capability.generate(signed)))[-1].kind == "completed"
    assert len(api.calls) == 2
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        read_credential(path)
    path.chmod(0o600)
    link = path.parent / "linked-key.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_credential(link)


async def test_canonical_proof_survives_policy_set_order_and_json_roundtrip(
    tmp_path: Path,
) -> None:
    from skulk_openai.admission import SignedInvocation

    capability, credential, catalog, _, clock = await prepared(tmp_path)
    original = invocation(catalog, clock)
    original = original.model_copy(
        update={
            "requirements": original.requirements.model_copy(
                update={"allowed_domains": frozenset({"external", "local"})}
            )
        }
    )
    signed = credential.sign(original)
    roundtrip = SignedInvocation.model_validate_json(signed.model_dump_json())
    assert (await collect(capability.generate(roundtrip)))[-1].kind == "completed"


async def test_proof_expiring_behind_database_writer_is_not_admitted(
    tmp_path: Path,
) -> None:
    capability, credential, catalog, api, clock = await prepared(tmp_path)
    started = threading.Event()

    def admission_clock() -> float:
        """Observe the real admission thread before it waits for the database."""
        started.set()
        return clock()

    capability.grants.clock = admission_clock
    signed = credential.sign(invocation(catalog, clock))
    blocker = sqlite3.connect(capability.grants.credentials.path)
    blocker.execute("BEGIN IMMEDIATE")
    pending = asyncio.create_task(
        asyncio.to_thread(
            capability.grants.admit,
            signed,
            catalog=catalog,
            target_node=capability.target_node,
            callable_id=capability.callable_id,
            descriptor_revision=capability.descriptor_revision,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        clock.now += 31
    finally:
        blocker.rollback()
        blocker.close()
    with pytest.raises(AuthFailure, match="permission_missing"):
        await pending
    fresh = credential.sign(invocation(catalog, clock))
    assert (await collect(capability.generate(fresh)))[-1].kind == "completed"
    assert len(api.calls) == 2
