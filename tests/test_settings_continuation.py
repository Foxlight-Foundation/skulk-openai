"""Owner settings round-trips preserve current non-gateway continuation custody."""

from pathlib import Path

from skulk_openai.checkpoints import CheckpointStore
from skulk_openai.management import ManagementServer
from tests.test_checkpoints import first, query
from tests.test_gateway_chat import configured
from tests.test_management import browser


async def test_unchanged_settings_preserve_receipt_without_gateway_key(
    tmp_path: Path,
) -> None:
    """A browser omits the hidden account fence without revoking unchanged policy."""
    capability, service, settings, _, _, _, clock = await configured(tmp_path)
    settings = settings.model_copy(update={"gateway_public_key": None})
    capability.settings.save(settings)
    store = CheckpointStore(
        capability.provider.sessions.store,
        target_node=capability.target_node,
        callable_id=service.callable_id,
        descriptor_revision=service.revision,
        clock=clock,
        retention_seconds=300,
        maximum_records=32,
        maximum_storage_bytes=64 * 1024 * 1024,
    )
    credential = store.issue(models=("text-a",), expires_at=clock() + 3600)
    receipt = first(store, credential)
    async with browser(ManagementServer(capability, checkpoints=store)) as client:
        state = (await client.get("/api/state")).json()
        assert "account_binding" not in state["settings"]
        response = await client.post("/api/settings", json=state["settings"])
        assert response.status_code == 200
        assert response.json()["settings"] == state["settings"]
        assert capability.settings.read() == settings
        assert query(store, credential, receipt) == receipt
        assert capability.provider.requests_started == 0


async def test_offline_enabled_save_without_gateway_cannot_approve_account(
    tmp_path: Path,
) -> None:
    """A missing gateway key must not let an enabled save skip account admission."""
    capability, _, settings, _, _, _, _ = await configured(tmp_path)
    settings = settings.model_copy(update={"gateway_public_key": None})
    capability.settings.save(settings)
    await capability.provider.sessions.disconnect()
    async with browser(ManagementServer(capability)) as client:
        state = (await client.get("/api/state")).json()
        response = await client.post("/api/settings", json=state["settings"])
        assert response.status_code == 400
        assert capability.settings.read() == settings
        assert capability.provider.requests_started == 0
