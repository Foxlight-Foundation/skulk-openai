"""Owner HTTP approval binds only client public keys; CLI secrets stay local."""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from skulk_inference_reference.continuation import ContinuationFailure
from skulk_inference_reference.continuation_auth import (
    ClientEnrollment,
    ClientRegistration,
    ContinuationKey,
    ContinuationProof,
)
from skulk_openai.capability import InferenceCapability
from skulk_openai.checkpoints import CheckpointStore
from skulk_openai.continuation_client import read_key, write_key
from skulk_openai.management import ManagementServer
from skulk_openai.settings import IntegrationSettings, account_binding
from tests.test_admission import prepared
from tests.test_management import browser
from tests.test_sessions import Clock

_OBJECT = TypeAdapter(dict[str, JsonValue])
_REPO = Path(__file__).resolve().parents[1]


async def cli(*arguments: str, stdin: bytes = b"") -> tuple[int, bytes, bytes]:
    """Run the account-free command with an environment containing no credentials."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "skulk_openai.continuation_client",
        *arguments,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.defpath, "PYTHONPATH": str(_REPO / "src")},
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(stdin), 5)
    assert process.returncode is not None
    return process.returncode, stdout, stderr


async def owner(tmp_path: Path) -> tuple[InferenceCapability, CheckpointStore, Clock]:
    """Prepare generated account policy, without issuing any continuation key."""
    capability, _, catalog, _, clock = await prepared(tmp_path)
    capability.settings.save(
        IntegrationSettings(
            inference_enabled=True,
            processing="allow_openai",
            usage="app_settings",
            models=("text-a",),
            account_binding=account_binding(
                catalog.registration_id, catalog.generation
            ),
        )
    )
    store = CheckpointStore(
        capability.grants.credentials,
        target_node="managed.reference",
        callable_id="fixture.continuation",
        descriptor_revision="b" * 16,
        clock=clock,
        retention_seconds=300,
        maximum_records=32,
        maximum_storage_bytes=64 * 1024 * 1024,
    )
    return capability, store, clock


async def test_owner_approves_public_key_and_client_cli_signs_exact_bytes(
    tmp_path: Path,
) -> None:
    capability, store, clock = await owner(tmp_path / "owner")
    key_file = tmp_path / "client-key.json"
    code, stdout, stderr = await cli("keygen", "--key-file", str(key_file))
    assert code == 0 and not stderr
    key = read_key(key_file)
    public = _OBJECT.validate_json(stdout)
    assert public == {"public_key": key.public_key}
    assert key_file.stat().st_mode & 0o077 == 0
    async with browser(ManagementServer(capability, checkpoints=store)) as client:
        response = await client.post(
            "/api/clients/register",
            json={**public, "models": ["text-a"], "expires_at": clock() + 300},
        )
        assert response.status_code == 200
        enrollment = ClientEnrollment.model_validate_json(response.content)
        assert enrollment.client.public_key == key.public_key
        assert key.private_key.get_secret_value() not in response.text
        duplicate = await client.post(
            "/api/clients/register",
            json={**public, "models": ["text-a"], "expires_at": clock() + 600},
        )
        assert duplicate.status_code == 409
        assert store.clients() == (enrollment.client,)
        listed = await client.get("/api/continuation")
        assert (
            listed.status_code == 200
            and listed.json()["clients"][0]["caller_id"] == enrollment.client.caller_id
        )
        assert "synthetic-subject" not in listed.text and "tokens" not in listed.text
        enrollment_file = tmp_path / "enrollment.json"
        enrollment_file.write_bytes(response.content)
        raw = b'{ "messages" : [ {"role":"user", "content":"hello"} ] }\n'
        args = (
            "--key-file",
            str(key_file),
            "--enrollment-file",
            str(enrollment_file),
            "--model",
            "text-a",
            "--conversation",
            "1" * 64,
        )
        code, stdout, stderr = await cli("sign", *args, stdin=raw)
        assert (
            code == 0
            and not stderr
            and key.private_key.get_secret_value().encode() not in stdout
        )
        proof = ContinuationProof.model_validate_json(stdout)
        with store.worker() as worker:
            lease = worker.begin(proof, raw)
            lease.close()
        assert (
            await client.post(
                "/api/clients/revoke", json={"caller_id": enrollment.client.caller_id}
            )
        ).status_code == 200
        assert not store.clients()
        with store.transaction() as connection:
            with pytest.raises(ContinuationFailure):
                store.authenticate(connection, proof)
    assert (
        key.private_key.get_secret_value().encode()
        not in store.credentials.path.read_bytes()
    )


@pytest.mark.parametrize(
    "change",
    ["csrf", "cookie", "model", "expired", "private_field", "duplicate_models"],
)
async def test_registration_refusal_never_mints_authority(
    tmp_path: Path, change: str
) -> None:
    capability, store, clock = await owner(tmp_path)
    request: dict[str, JsonValue] = {
        "public_key": ContinuationKey.generate().public_key,
        "models": ["text-a"],
        "expires_at": clock() + 300,
    }
    async with browser(ManagementServer(capability, checkpoints=store)) as client:
        headers: dict[str, str] = {}
        if change == "csrf":
            headers["X-CSRF-Token"] = "wrong"
        elif change == "cookie":
            client.cookies.clear()
        elif change == "model":
            request["models"] = ["not-approved"]
        elif change == "expired":
            request["expires_at"] = clock() - 1
        elif change == "private_field":
            request["private_key"] = "forbidden"
        else:
            request["models"] = ["text-a", "text-a"]
        response = await client.post(
            "/api/clients/register", json=request, headers=headers
        )
        assert response.status_code in {400, 403, 409}
        assert not store.clients()
        assert capability.provider.requests_started == 0


async def test_client_key_cannot_be_overwritten_symlinked_or_rebound(
    tmp_path: Path,
) -> None:
    key = ContinuationKey.generate()
    path = tmp_path / "key.json"
    write_key(path, key)
    with pytest.raises(FileExistsError):
        write_key(path, ContinuationKey.generate())
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_key(path)
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_key(link)
    request = ClientRegistration(
        public_key=ContinuationKey.generate().public_key,
        models=("text-a",),
        expires_at=10000000000.0,
    )
    from skulk_inference_reference.continuation_auth import RegisteredClient

    with pytest.raises(ValueError):
        key.bind(RegisteredClient(caller_id="1" * 32, **request.model_dump()))


async def test_disabled_continuation_does_not_create_enrollment_storage(
    tmp_path: Path,
) -> None:
    capability, _, _, _, clock = await prepared(tmp_path)
    async with browser(ManagementServer(capability)) as client:
        assert (await client.get("/api/continuation")).json() == {
            "configured": False,
            "target": None,
            "clients": [],
        }
        response = await client.post(
            "/api/clients/register",
            json={
                "public_key": ContinuationKey.generate().public_key,
                "models": ["text-a"],
                "expires_at": clock() + 300,
            },
        )
        assert response.status_code == 409
    assert not (capability.grants.credentials.root / "continuation.key").exists()
