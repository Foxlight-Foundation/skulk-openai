"""Generated OIDC identities exercise sessions without an OpenAI account."""

import asyncio
import base64
import hashlib
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import JsonValue, TypeAdapter

from skulk_openai.auth_contracts import ISSUER, PLAN_SCOPE, AuthFailure
from skulk_openai.credentials import CredentialStore
from skulk_openai.oauth import AuthorizationAttempt, OAuthClient
from skulk_openai.sessions import SessionManager
from skulk_openai.setup import CallbackListener

_OBJECT = TypeAdapter(dict[str, JsonValue])


class Clock:
    """A controlled runtime clock; JWT validation uses actual signed expiry."""

    def __init__(self) -> None:
        """Start near actual UTC for the independently validated JWT lifetime."""
        self.now = time.time()

    def __call__(self) -> float:
        """Return the injected session-clock value."""
        return self.now


class Issuer:
    """A synthetic fixed-origin issuer with generated signing keys and rotation."""

    def __init__(self, clock: Clock) -> None:
        """Initialize controlled endpoints and no real credentials."""
        self.clock = clock
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        raw = RSAAlgorithm.to_jwk(self.key.public_key())
        assert isinstance(raw, str)
        self.public = _OBJECT.validate_json(raw)
        self.public.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
        self.attempt: AuthorizationAttempt | None = None
        self.scopes = "openid offline_access " + PLAN_SCOPE
        self.failure: str | None = None
        self.refresh_calls = 0
        self.refresh_value = "synthetic-refresh-1"
        self.revoked: list[str] = []
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None
        self.claim_overrides: dict[str, object] = {}
        self.requests: list[httpx.Request] = []
        self.refresh_identity = False
        self.refresh_error = "invalid_grant"

    def identity(self, client_id: str) -> str:
        """Sign a generated identity; no trusted field comes from callback email."""
        assert self.attempt is not None
        claims: dict[str, object] = {
            "iss": ISSUER,
            "sub": "synthetic-subject",
            "aud": client_id,
            "iat": int(time.time()) - 1,
            "exp": int(time.time()) + 3600,
            "nonce": self.attempt.nonce.get_secret_value(),
            **self.claim_overrides,
        }
        return jwt.encode(
            claims, self.key, algorithm="RS256", headers={"kid": "test-key"}
        )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        """Serve discovery, keys, code exchange, refresh and revocation in memory."""
        self.requests.append(request)
        assert request.url.host == "auth.openai.com"
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": ISSUER + "/api/accounts/authorize",
                    "token_endpoint": ISSUER + "/api/accounts/oauth/token",
                    "jwks_uri": ISSUER + "/.well-known/jwks.json",
                    "revocation_endpoint": ISSUER + "/api/accounts/oauth/revoke",
                },
            )
        if request.url.path == "/.well-known/jwks.json":
            if self.failure == "jwks":
                return httpx.Response(503)
            return httpx.Response(200, json={"keys": [self.public]})
        form = parse_qs(request.content.decode())
        if request.url.path == "/api/accounts/oauth/revoke":
            self.revoked.append(form["token"][0])
            return httpx.Response(503 if self.failure == "revoke" else 200)
        assert request.url.path == "/api/accounts/oauth/token"
        assert form["resource"] == ["https://api.openai.com/v1"]
        if form["grant_type"] == ["authorization_code"]:
            assert self.attempt is not None
            assert form["redirect_uri"] == [self.attempt.redirect_uri]
            parameters = parse_qs(urlsplit(self.attempt.url.get_secret_value()).query)
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(form["code_verifier"][0].encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            assert parameters["code_challenge"] == [challenge]
            return httpx.Response(
                200,
                json={
                    "access_token": "synthetic-access-1",
                    "refresh_token": self.refresh_value,
                    "id_token": self.identity(form["client_id"][0]),
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": self.scopes,
                },
            )
        assert form["grant_type"] == ["refresh_token"] and "scope" not in form
        self.refresh_calls += 1
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.failure == "refresh":
            return httpx.Response(503, json={"message": "private synthetic detail"})
        if self.failure == "revoked":
            return httpx.Response(400, json={"error": self.refresh_error})
        assert form["refresh_token"] == [self.refresh_value]
        self.refresh_value = f"synthetic-refresh-{self.refresh_calls + 1}"
        identity = (
            {"id_token": self.identity(form["client_id"][0])}
            if self.refresh_identity
            else {}
        )
        if self.refresh_identity:
            self.failure = "jwks"
        return httpx.Response(
            200,
            json={
                "access_token": "synthetic-access-rotated",
                "refresh_token": self.refresh_value,
                "token_type": "Bearer",
                "expires_in": 3600,
                **identity,
            },
        )


def setup(tmp_path: Path) -> tuple[SessionManager, Issuer, Clock]:
    """Create an isolated capability store and explicit mock transport."""
    clock = Clock()
    issuer = Issuer(clock)
    manager = SessionManager(
        CredentialStore(tmp_path / "provider"),
        OAuthClient(transport=httpx.MockTransport(issuer.handle), clock=clock),
        clock=clock,
    )
    return manager, issuer, clock


async def test_closing_listener_discards_only_its_pending_attempt(
    tmp_path: Path,
) -> None:
    manager, _, _ = setup(tmp_path)
    retained = CallbackListener(manager)
    await retained.start()
    assert retained.attempt is not None
    try:
        for _ in range(8):
            abandoned = CallbackListener(manager)
            await abandoned.start()
            assert abandoned.attempt is not None
            assert manager.client.is_pending(abandoned.attempt)
            await abandoned.close()
            assert not manager.client.is_pending(abandoned.attempt)
            assert manager.client.is_pending(retained.attempt)
    finally:
        await retained.close()
    assert not manager.client.is_pending(retained.attempt)


async def connect(
    manager: SessionManager, issuer: Issuer, *, registration_id: str | None = None
) -> None:
    """Complete generated consent through the same PKCE/code path as owner setup."""
    issuer.attempt = await manager.begin(
        "http://127.0.0.1:1455/auth/callback", registration_id=registration_id
    )
    query = urlencode(
        {
            "state": issuer.attempt.state.get_secret_value(),
            "code": "synthetic-code",
            "client_id": "oaiapp_test",
        }
    )
    await manager.complete(issuer.attempt, query)


async def test_connection_survives_restart_with_stable_host_identity(
    tmp_path: Path,
) -> None:
    manager, issuer, _ = setup(tmp_path)
    host = manager.store.host_id()
    assert host.startswith("urn:uuid:")
    await connect(manager, issuer)
    status = await manager.status()
    assert status.state == "authorized" and status.registration_id is not None
    restarted = SessionManager(CredentialStore(tmp_path / "provider"), manager.client)
    assert restarted.store.host_id() == host
    assert (await restarted.status()).registration_id == status.registration_id
    assert (manager.store.path.stat().st_mode & 0o777) == 0o600
    assert (manager.store.root.stat().st_mode & 0o777) == 0o700
    assert "synthetic-access" not in repr(manager.store.load())
    assert "synthetic-refresh" not in repr(manager.store.load())


async def test_identity_only_does_not_admit_inference(tmp_path: Path) -> None:
    manager, issuer, _ = setup(tmp_path)
    issuer.scopes = "openid offline_access"
    await connect(manager, issuer)
    status = await manager.status()
    assert status.state == "identity_only" and status.registration_id is not None
    with pytest.raises(AuthFailure, match="permission_missing"):
        await manager.access_token(status.registration_id, status.generation or 0)
    assert issuer.refresh_calls == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://attacker.invalid"},
        {"sub": ""},
        {"aud": "wrong-client"},
        {"exp": 1},
        {"iat": 9_999_999_999},
        {"nonce": "wrong-nonce"},
        {"azp": "another-client"},
        {"aud": ["oaiapp_test", "other"]},
    ],
)
async def test_identity_failure_never_persists_tokens(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    manager, issuer, _ = setup(tmp_path)
    issuer.claim_overrides = overrides
    with pytest.raises(AuthFailure, match="identity_invalid"):
        await connect(manager, issuer)
    assert (await manager.status()).state == "disconnected"
    assert manager.store.load() is None


async def test_refresh_is_serialized_and_atomically_rotated(tmp_path: Path) -> None:
    manager, issuer, clock = setup(tmp_path)
    await connect(manager, issuer)
    status = await manager.status()
    assert status.registration_id is not None and status.generation is not None
    clock.now += 3550
    values = await asyncio.gather(
        *(
            manager.access_token(status.registration_id, status.generation)
            for _ in range(4)
        )
    )
    assert len(values) == 4 and issuer.refresh_calls == 1
    saved = manager.store.load()
    assert (
        saved is not None
        and saved.tokens is not None
        and saved.tokens.refresh_token is not None
    )
    assert saved.tokens.refresh_token.get_secret_value() == issuer.refresh_value


async def test_transient_refresh_failure_preserves_credentials(tmp_path: Path) -> None:
    manager, issuer, clock = setup(tmp_path)
    await connect(manager, issuer)
    old = manager.store.load()
    status = await manager.status()
    assert status.registration_id is not None and status.generation is not None
    clock.now += 3550
    issuer.failure = "refresh"
    with pytest.raises(AuthFailure, match="provider_unavailable"):
        await manager.access_token(status.registration_id, status.generation)
    assert manager.store.load() == old
    issuer.failure = None
    assert await manager.access_token(status.registration_id, status.generation)


@pytest.mark.parametrize(
    "code",
    [
        "invalid_grant",
        "invalid_refresh_token",
        "token_expired",
        "refresh_token_expired",
        "refresh_token_invalidated",
        "refresh_token_reused",
    ],
)
async def test_revoked_refresh_withdraws_tokens(tmp_path: Path, code: str) -> None:
    manager, issuer, clock = setup(tmp_path)
    await connect(manager, issuer)
    status = await manager.status()
    assert status.registration_id is not None and status.generation is not None
    clock.now += 3550
    issuer.failure = "revoked"
    issuer.refresh_error = code
    with pytest.raises(AuthFailure, match="session_revoked"):
        await manager.access_token(status.registration_id, status.generation)
    assert (await manager.status()).state == "disconnected"
    saved = manager.store.load()
    assert saved is not None and saved.tokens is None
    with pytest.raises(AuthFailure, match="sign_in_required"):
        await manager.access_token(status.registration_id, status.generation)
    assert issuer.refresh_calls == 1


async def test_disconnect_fences_inflight_refresh(tmp_path: Path) -> None:
    manager, issuer, clock = setup(tmp_path)
    await connect(manager, issuer)
    status = await manager.status()
    assert status.registration_id is not None and status.generation is not None
    clock.now += 3550
    issuer.release = asyncio.Event()
    execution = asyncio.create_task(
        manager.access_token(status.registration_id, status.generation)
    )
    await asyncio.wait_for(issuer.entered.wait(), 1)
    assert await manager.disconnect()
    issuer.release.set()
    with pytest.raises(AuthFailure, match="stale_authorization"):
        await execution
    assert (await manager.status()).state == "disconnected"
    assert issuer.refresh_value in issuer.revoked


async def test_disconnect_fences_pending_first_authorization(tmp_path: Path) -> None:
    manager, issuer, _ = setup(tmp_path)
    issuer.attempt = await manager.begin("http://127.0.0.1:1455/auth/callback")
    assert await manager.disconnect()
    with pytest.raises(AuthFailure, match="stale_authorization"):
        await manager.complete(
            issuer.attempt,
            urlencode(
                {
                    "state": issuer.attempt.state.get_secret_value(),
                    "code": "synthetic-code",
                    "client_id": "oaiapp_test",
                }
            ),
        )
    assert (await manager.status()).state == "disconnected"


async def test_disconnect_keeps_registration_but_reports_unconfirmed_remote_revocation(
    tmp_path: Path,
) -> None:
    manager, issuer, _ = setup(tmp_path)
    await connect(manager, issuer)
    original = manager.store.load()
    assert original is not None
    host = manager.store.host_id()
    issuer.failure = "revoke"
    assert not await manager.disconnect()
    saved = manager.store.load()
    assert (
        saved is not None
        and saved.tokens is None
        and saved.client_id == original.client_id
    )
    assert manager.store.host_id() == host
    issuer.attempt = await manager.begin(
        "http://127.0.0.1:2444/auth/callback", registration_id=saved.registration_id
    )
    parameters = parse_qs(urlsplit(issuer.attempt.url.get_secret_value()).query)
    assert parameters["client_id"] == [saved.client_id]
    assert "agent_name_hint" not in parameters and "id_token_hint" not in parameters


async def test_callback_is_single_use_and_state_is_checked_before_http(
    tmp_path: Path,
) -> None:
    manager, issuer, _ = setup(tmp_path)
    attempt = await manager.begin("http://127.0.0.1:1455/auth/callback")
    with pytest.raises(AuthFailure, match="state_mismatch"):
        manager.client.consume(attempt, "state=wrong&code=x&client_id=oaiapp_test")
    assert issuer.requests == []
    query = urlencode(
        {
            "state": attempt.state.get_secret_value(),
            "code": "x",
            "client_id": "oaiapp_test",
        }
    )
    assert manager.client.consume(attempt, query) == ("x", "oaiapp_test")
    with pytest.raises(AuthFailure, match="invalid_callback"):
        manager.client.consume(attempt, query)


@pytest.mark.parametrize(
    "uri",
    [
        "http://localhost:1455/auth/callback",
        "https://127.0.0.1:1455/auth/callback",
        "http://127.0.0.1:1455/callback",
        "http://127.0.0.1:1455/auth/callback?x=1",
        "http://user@127.0.0.1:1455/auth/callback",
        "http://127.0.0.1:bad/auth/callback",
        "http://127.0.0.1:0/auth/callback",
    ],
)
async def test_callback_cannot_change_host_scheme_or_path(
    tmp_path: Path, uri: str
) -> None:
    manager, _, _ = setup(tmp_path)
    with pytest.raises(AuthFailure, match="invalid_callback"):
        await manager.begin(uri)


def test_loose_permissions_and_symlink_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "provider"
    root.mkdir(mode=0o755)
    with pytest.raises(AuthFailure, match="credentials_unavailable"):
        CredentialStore(root)
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(AuthFailure):
        CredentialStore(link)


async def test_real_loopback_callback_keeps_tokens_out_of_browser_response(
    tmp_path: Path,
) -> None:
    manager, issuer, _ = setup(tmp_path)
    listener = CallbackListener(manager)
    await listener.start()
    issuer.attempt = listener.attempt
    assert issuer.attempt is not None
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as browser:
        launch = await browser.get(listener.launch_url)
        assert launch.status_code == 302
        assert launch.headers["location"].startswith(
            ISSUER + "/api/accounts/authorize?"
        )
        assert "no-store" in launch.headers["cache-control"]
        assert (await browser.get(listener.launch_url)).status_code == 404
        wrong = await browser.get(listener.redirect_uri + "?state=wrong&code=x")
        assert wrong.status_code == 400 and issuer.requests == []
        malformed = await browser.get(listener.redirect_uri + "?state=one&state=two")
        assert malformed.status_code == 400
        query = urlencode(
            {
                "state": issuer.attempt.state.get_secret_value(),
                "code": "synthetic-code",
                "client_id": "oaiapp_test",
            }
        )
        callback = await browser.get(listener.redirect_uri + "?" + query)
    assert (await listener.wait()).state == "authorized"
    assert callback.status_code == 200
    assert "synthetic" not in callback.text
    assert "oaiapp" not in callback.text
    assert manager.store.load() is not None
    assert listener.server is not None and not listener.server.is_serving()


async def test_loopback_rejects_forged_host_and_cleans_up_on_timeout(
    tmp_path: Path,
) -> None:
    manager, issuer, _ = setup(tmp_path)
    listener = CallbackListener(manager)
    await listener.start()
    address = urlsplit(listener.redirect_uri)
    reader, writer = await asyncio.open_connection(address.hostname, address.port)
    writer.write(
        b"GET /auth/callback?state=wrong HTTP/1.1\r\nHost: attacker.invalid\r\n\r\n"
    )
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), 1) == b""
    writer.close()
    await writer.wait_closed()
    with pytest.raises(TimeoutError):
        await listener.wait(deadline_seconds=0.01)
    assert issuer.requests == []
    assert listener.server is not None and not listener.server.is_serving()


async def test_consent_denial_is_terminal_without_code_exchange(tmp_path: Path) -> None:
    manager, issuer, _ = setup(tmp_path)
    listener = CallbackListener(manager)
    await listener.start()
    assert listener.attempt is not None
    query = urlencode(
        {
            "state": listener.attempt.state.get_secret_value(),
            "error": "access_denied",
        }
    )
    async with httpx.AsyncClient(trust_env=False) as browser:
        assert (
            await browser.get(listener.redirect_uri + "?" + query)
        ).status_code == 400
    with pytest.raises(AuthFailure, match="consent_denied"):
        await listener.wait()
    assert issuer.requests == [] and manager.store.load() is None


async def test_stale_refresh_failure_cannot_disconnect_reauthorized_session(
    tmp_path: Path,
) -> None:
    manager, issuer, clock = setup(tmp_path)
    await connect(manager, issuer)
    old = manager.store.load()
    assert old is not None
    clock.now += 3550
    issuer.release = asyncio.Event()
    refresh = asyncio.create_task(
        manager.access_token(old.registration_id, old.generation)
    )
    await asyncio.wait_for(issuer.entered.wait(), 1)
    issuer.attempt = await manager.begin(
        "http://127.0.0.1:1456/auth/callback", registration_id=old.registration_id
    )
    await manager.complete(
        issuer.attempt,
        urlencode({"state": issuer.attempt.state.get_secret_value(), "code": "new"}),
    )
    issuer.failure = "revoked"
    issuer.release.set()
    with pytest.raises(AuthFailure, match="session_revoked"):
        await refresh
    current = manager.store.load()
    assert current is not None and current.generation > old.generation
    assert current.enabled and current.tokens is not None
    assert (await manager.status()).state == "authorized"


async def test_key_fetch_failure_happens_before_rotating_refresh(
    tmp_path: Path,
) -> None:
    manager, issuer, clock = setup(tmp_path)
    await connect(manager, issuer)
    current = manager.store.load()
    assert current is not None
    clock.now += 3550
    issuer.failure = "jwks"
    with pytest.raises(AuthFailure, match="provider_unavailable"):
        await manager.access_token(current.registration_id, current.generation)
    assert issuer.refresh_calls == 0
    issuer.failure = None
    assert await manager.access_token(current.registration_id, current.generation)
    assert issuer.refresh_calls == 1


async def test_refresh_persists_rotation_without_a_subsequent_key_request(
    tmp_path: Path,
) -> None:
    manager, issuer, clock = setup(tmp_path)
    await connect(manager, issuer)
    current = manager.store.load()
    assert current is not None
    clock.now += 3550
    issuer.refresh_identity = True
    assert await manager.access_token(current.registration_id, current.generation)
    saved = manager.store.load()
    assert saved is not None and saved.tokens is not None
    assert saved.tokens.refresh_token is not None
    assert saved.tokens.refresh_token.get_secret_value() == issuer.refresh_value
    assert issuer.requests[-1].url.path == "/api/accounts/oauth/token"


@pytest.mark.parametrize("fault", ["signature", "key-id"])
async def test_invalid_signatures_or_unknown_keys_cannot_save_a_connection(
    tmp_path: Path, fault: str
) -> None:
    manager, issuer, _ = setup(tmp_path)
    if fault == "signature":
        issuer.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    else:
        issuer.public["kid"] = "unrecognized-key"
    with pytest.raises(AuthFailure, match="identity_invalid"):
        await connect(manager, issuer)
    assert manager.store.load() is None
