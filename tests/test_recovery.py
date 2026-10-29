"""Explicit permission recovery and terminal refresh errors never consume inference."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from skulk_openai.auth_contracts import AuthFailure
from skulk_openai.oauth import OAuthClient
from skulk_openai.setup import CallbackListener
from tests.test_sessions import connect, setup


async def test_permission_recovery_is_explicit_and_reuses_the_issued_client(
    tmp_path: Path,
) -> None:
    manager, issuer, _ = setup(tmp_path)
    issuer.scopes = "openid profile email"
    await connect(manager, issuer)
    selected = manager.store.load()
    assert selected is not None
    ordinary = await manager.begin(
        "http://127.0.0.1:1455/auth/callback",
        registration_id=selected.registration_id,
    )
    assert "prompt" not in parse_qs(urlsplit(ordinary.url.get_secret_value()).query)
    prior_requests = len(issuer.requests)
    listener = CallbackListener(manager)
    await listener.start(
        registration_id=selected.registration_id, request_plan_consent=True
    )
    try:
        assert listener.attempt is not None
        parameters = parse_qs(urlsplit(listener.attempt.url.get_secret_value()).query)
        assert parameters["client_id"] == [selected.client_id]
        assert parameters["prompt"] == ["consent"]
        assert "chatgpt.tokens.use.direct" in parameters["scope"][0].split()
        assert "force_reconsent" not in parameters
        assert "agent_name_hint" not in parameters
        assert listener.attempt.state != ordinary.state
        assert len(issuer.requests) == prior_requests
        assert manager.store.load() == selected
    finally:
        await listener.close()


@pytest.mark.parametrize("nested", [False, True])
async def test_refresh_error_body_shapes_clear_only_confirmed_unusable_tokens(
    tmp_path: Path, nested: bool
) -> None:
    manager, issuer, clock = setup(tmp_path)
    await connect(manager, issuer)
    selected = manager.store.load()
    assert selected is not None
    clock.now += 3550

    async def response(request: httpx.Request) -> httpx.Response:
        """Wrap the controlled refresh error, preserving generated key checks."""
        if b"grant_type=refresh_token" in request.content:
            return httpx.Response(
                401,
                json={
                    "error": {"code": "refresh_token_reused"}
                    if nested
                    else "refresh_token_reused"
                },
            )
        return await issuer.handle(request)

    manager.client = OAuthClient(transport=httpx.MockTransport(response), clock=clock)
    with pytest.raises(AuthFailure, match="session_revoked"):
        await manager.access_token(selected.registration_id, selected.generation)
    assert (await manager.status()).state == "disconnected"


@pytest.mark.parametrize("status", [400, 503])
async def test_invalid_client_or_infrastructure_failure_preserves_usable_storage(
    tmp_path: Path, status: int
) -> None:
    manager, issuer, clock = setup(tmp_path)
    await connect(manager, issuer)
    selected = manager.store.load()
    assert selected is not None
    clock.now += 3550

    async def response(request: httpx.Request) -> httpx.Response:
        """A 5xx cannot turn a terminal-looking body into local logout."""
        if b"grant_type=refresh_token" in request.content:
            return httpx.Response(
                status,
                json={"error": "invalid_client" if status == 400 else "invalid_grant"},
            )
        return await issuer.handle(request)

    manager.client = OAuthClient(transport=httpx.MockTransport(response), clock=clock)
    with pytest.raises(AuthFailure):
        await manager.access_token(selected.registration_id, selected.generation)
    assert manager.store.load() == selected
