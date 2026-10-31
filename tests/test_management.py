"""Real loopback HTTP proves owner authority, bounded grants and safe UI assets."""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from pydantic import JsonValue, TypeAdapter

from skulk_openai.capability import InferenceCapability
from skulk_openai.management import ManagementServer
from skulk_openai.oauth import AuthorizationAttempt
from skulk_openai.provider import ProviderClient
from skulk_openai.responses import event_bytes
from tests.test_admission import prepared
from tests.test_sessions import setup

_OBJECT = TypeAdapter(dict[str, JsonValue])


@asynccontextmanager
async def browser(server: ManagementServer) -> AsyncGenerator[httpx.AsyncClient]:
    """Bootstrap through the same public root a host browser opens."""
    await server.start()
    try:
        async with httpx.AsyncClient(base_url=server.url, trust_env=False) as client:
            root = await client.get("/")
            assert root.status_code == 200
            assert "HttpOnly" in root.headers["set-cookie"]
            assert "SameSite=Strict" in root.headers["set-cookie"]
            state = _OBJECT.validate_json((await client.get("/api/state")).content)
            assert isinstance(state["csrf"], str)
            client.headers.update({"Origin": server.url, "X-CSRF-Token": state["csrf"]})
            yield client
    finally:
        await server.close()


def payload(revision: str) -> dict[str, JsonValue]:
    """Declare explicit external usage for exactly one bounded test."""
    return {
        "model": "text-a",
        "prompt": "private input",
        "catalog_revision": revision,
        "processing": "allow_openai",
        "usage": "app_settings",
    }


async def test_assets_and_cached_status_do_not_contact_provider(tmp_path: Path) -> None:
    capability, _, _, api, _ = await prepared(tmp_path)
    async with browser(ManagementServer(capability)) as client:
        for path, media in [
            ("/", "text/html"),
            ("/assets/app.css", "text/css"),
            ("/assets/app.js", "text/javascript"),
        ]:
            response = await client.get(path)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(media)
            assert response.headers["cache-control"] == "no-store"
            assert (
                "frame-ancestors 'none'" in response.headers["content-security-policy"]
            )
            for private in ("synthetic-access", "synthetic-subject", str(tmp_path)):
                assert private not in response.text
        state = _OBJECT.validate_json((await client.get("/api/state")).content)
        assert state["connection"] == "authorized" and state["models"]
        assert not {
            "registration_id",
            "generation",
            "tokens",
            "state_directory",
        }.intersection(state)
        assert len(api.calls) == 1


@pytest.mark.parametrize(
    "failure", ["cookie", "csrf", "origin", "site", "forwarded", "host", "media"]
)
async def test_mutations_require_local_owner_session_and_csrf(
    tmp_path: Path, failure: str
) -> None:
    capability, _, _, api, _ = await prepared(tmp_path)
    async with browser(ManagementServer(capability)) as client:
        headers: dict[str, str] = {}
        if failure == "cookie":
            client.cookies.clear()
        elif failure == "csrf":
            headers["X-CSRF-Token"] = "wrong"
        elif failure == "origin":
            headers["Origin"] = "https://evil.example"
        elif failure == "site":
            headers["Sec-Fetch-Site"] = "cross-site"
        elif failure == "forwarded":
            headers["Forwarded"] = "for=127.0.0.1"
        elif failure == "host":
            headers["Host"] = "evil.example"
        else:
            headers["Content-Type"] = "text/plain"
        response = await client.post("/api/disconnect", json={}, headers=headers)
        assert response.status_code in {400, 403}
        assert (await capability.provider.sessions.status()).state == "authorized"
        assert len(api.calls) == 1


async def test_plain_get_cannot_mutate_or_read_without_session(tmp_path: Path) -> None:
    capability, _, _, api, _ = await prepared(tmp_path)
    async with browser(ManagementServer(capability)) as client:
        assert (await client.get("/api/disconnect")).status_code == 404
        client.cookies.clear()
        assert (await client.get("/api/state")).status_code == 403
        assert (await capability.provider.sessions.status()).state == "authorized"
        assert len(api.calls) == 1


@pytest.mark.parametrize(
    "change", [{"processing": "local_only"}, {"usage": "included_only"}]
)
async def test_hard_policy_rejects_before_any_outbound_request(
    tmp_path: Path, change: dict[str, str]
) -> None:
    capability, _, catalog, api, _ = await prepared(tmp_path)
    async with browser(ManagementServer(capability)) as client:
        response = await client.post(
            "/api/generate", json={**payload(catalog.revision), **change}
        )
        assert response.status_code == 403
        assert response.json() == {"error": "policy_denied"}
        assert len(api.calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"model": "hidden"},
        {"catalog_revision": "b" * 64},
        {"prompt": ""},
        {"prompt": "x" * 4097},
        {"extra": "no"},
    ],
)
async def test_invalid_or_stale_test_keeps_content_local(
    tmp_path: Path, change: dict[str, str]
) -> None:
    capability, _, catalog, api, _ = await prepared(tmp_path)
    async with browser(ManagementServer(capability)) as client:
        response = await client.post(
            "/api/generate", json={**payload(catalog.revision), **change}
        )
        assert response.status_code == 400 or '"kind":"failed"' in response.text
        assert len(api.calls) == 1


async def test_owner_test_uses_one_finite_admission_and_revokes_it(
    tmp_path: Path,
) -> None:
    capability, old, catalog, api, _ = await prepared(tmp_path)
    capability.grants.revoke(old.grant.grant_id)
    async with browser(ManagementServer(capability)) as client:
        response = await client.post("/api/generate", json=payload(catalog.revision))
        assert response.status_code == 200
        events = [_OBJECT.validate_json(line) for line in response.text.splitlines()]
        assert [item["kind"] for item in events] == ["text", "completed"]
        assert events[0]["text"] == "hello" and len(api.calls) == 2
        with capability.grants.credentials.transaction() as connection:
            rows = connection.execute(
                "SELECT remaining,enabled,policy FROM grants WHERE id!=?",
                (old.grant.grant_id,),
            ).fetchall()
            assert len(rows) == 1 and tuple(rows[0][:2]) == (0, 0)
            assert json.loads(str(rows[0][2]))["maximum_attempts"] == 1
            assert (
                connection.execute("SELECT COUNT(*) FROM grant_nonces").fetchone()[0]
                == 1
            )
        assert b"private input" not in capability.grants.credentials.path.read_bytes()
        assert "signature" not in response.text and "private_key" not in response.text


async def test_refresh_and_disconnect_are_explicit_owner_actions(
    tmp_path: Path,
) -> None:
    capability, _, _, api, _ = await prepared(tmp_path)
    async with browser(ManagementServer(capability)) as client:
        assert (await client.post("/api/models", json={})).status_code == 200
        assert len(api.calls) == 2
        result = await client.post("/api/disconnect", json={})
        assert result.status_code == 200 and result.json()["revocation_confirmed"]
        assert (await client.get("/api/state")).json()["models"] == []
        assert (await capability.discovery()).offers == ()


async def test_revocation_failure_reports_local_fence(tmp_path: Path) -> None:
    sessions, issuer, clock = setup(tmp_path)
    from tests.test_sessions import connect

    await connect(sessions, issuer)
    issuer.failure = "revoke"
    capability = InferenceCapability(
        ProviderClient(
            sessions, transport=httpx.MockTransport(lambda _: httpx.Response(503))
        ),
        target_node="local",
        callable_id="test",
        descriptor_revision="a" * 16,
        clock=clock,
    )
    async with browser(ManagementServer(capability)) as client:
        result = await client.post("/api/disconnect", json={})
        assert result.json() == {"revocation_confirmed": False}
        assert (await client.get("/api/state")).json()[
            "notice"
        ] == "revocation_unconfirmed"
        assert (await sessions.status()).state == "disconnected"


async def test_web_consent_uses_existing_verified_loopback_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, issuer, clock = setup(tmp_path)
    begin = sessions.begin

    async def capture(
        redirect_uri: str,
        *,
        registration_id: str | None = None,
        request_plan_consent: bool = False,
    ) -> AuthorizationAttempt:
        attempt = await begin(
            redirect_uri,
            registration_id=registration_id,
            request_plan_consent=request_plan_consent,
        )
        issuer.attempt = attempt
        return attempt

    monkeypatch.setattr(sessions, "begin", capture)
    capability = InferenceCapability(
        ProviderClient(
            sessions, transport=httpx.MockTransport(lambda _: httpx.Response(503))
        ),
        target_node="local",
        callable_id="test",
        descriptor_revision="a" * 16,
        clock=clock,
    )
    server = ManagementServer(capability)
    async with browser(server) as client:
        response = await client.post("/api/connect", json={})
        launch = response.json()["launch_url"]
        assert launch.startswith("http://127.0.0.1:") and "id_token" not in launch
        assert issuer.attempt is not None
        attempt = issuer.attempt
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as local:
            redirect = await local.get(launch)
            assert redirect.status_code == 302
            query = urlencode(
                {
                    "state": attempt.state.get_secret_value(),
                    "code": "synthetic-code",
                    "client_id": "oaiapp_test",
                }
            )
            callback = await local.get(attempt.redirect_uri + "?" + query)
            assert callback.status_code == 200
            assert "Connection saved" in callback.text
        async with asyncio.timeout(2):
            while True:
                state = (await client.get("/api/state")).json()
                if state["notice"] == "connected":
                    break
                await asyncio.sleep(0.01)
        assert state["connection"] == "authorized" and state["notice"] == "connected"


class SlowText(httpx.AsyncByteStream):
    """Hold a real HTTP response iterator until cancellation, with no real service."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()
        self.release = asyncio.Event()

    async def __aiter__(self) -> AsyncGenerator[bytes]:
        yield event_bytes({"type": "response.output_text.delta", "delta": "first"})
        self.entered.set()
        await self.release.wait()
        yield event_bytes({"type": "response.completed"})

    async def aclose(self) -> None:
        self.closed.set()


async def test_cancel_keeps_management_responsive_and_revokes_budget(
    tmp_path: Path,
) -> None:
    capability, old, catalog, _, _ = await prepared(tmp_path)
    capability.grants.revoke(old.grant.grant_id)
    slow = SlowText()
    capability.provider.transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=slow
        )
    )
    async with browser(ManagementServer(capability)) as client:
        running = asyncio.create_task(
            client.post("/api/generate", json=payload(catalog.revision))
        )
        await asyncio.wait_for(slow.entered.wait(), 2)
        assert (await client.get("/api/state")).json()["busy"]
        assert (
            await client.post("/api/generate", json=payload(catalog.revision))
        ).status_code == 409
        assert (await client.post("/api/cancel", json={})).status_code == 200
        response = await asyncio.wait_for(running, 2)
        assert '"error":"interrupted"' in response.text
        assert slow.closed.is_set()
        with capability.grants.credentials.transaction() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM grants WHERE enabled=1"
                ).fetchone()[0]
                == 0
            )


async def test_browser_disconnect_cancels_upstream_and_revokes_grant(
    tmp_path: Path,
) -> None:
    capability, old, catalog, _, _ = await prepared(tmp_path)
    capability.grants.revoke(old.grant.grant_id)
    slow = SlowText()
    capability.provider.transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=slow
        )
    )
    async with browser(ManagementServer(capability)) as client:
        async with client.stream(
            "POST", "/api/generate", json=payload(catalog.revision)
        ) as response:
            first = await anext(response.aiter_lines())
            assert '"text":"first"' in first
        await asyncio.wait_for(slow.closed.wait(), 2)
        async with asyncio.timeout(2):
            for _ in range(50):
                if not (await client.get("/api/state")).json()["busy"]:
                    break
            else:
                raise AssertionError("browser disconnect did not release the test")
        with capability.grants.credentials.transaction() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM grants WHERE enabled=1"
                ).fetchone()[0]
                == 0
            )


@pytest.mark.parametrize("disabled", [True, False])
async def test_disabled_or_expired_catalog_never_sends_prompt(
    tmp_path: Path, disabled: bool
) -> None:
    capability, _, catalog, api, clock = await prepared(tmp_path)
    if disabled:
        capability.enabled = False
    else:
        clock.now += 61
    async with browser(ManagementServer(capability)) as client:
        response = await client.post("/api/generate", json=payload(catalog.revision))
        assert response.status_code == 403 or '"error":"policy_denied"' in response.text
        assert len(api.calls) == 1
        state = (await client.get("/api/state")).json()
        assert state["inference_enabled"] is (not disabled)


async def test_dashboard_navigation_cannot_authorize_cross_site_actions(
    tmp_path: Path,
) -> None:
    capability, _, _, api, _ = await prepared(tmp_path)
    async with browser(ManagementServer(capability)) as client:
        headers = {
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        }
        assert (await client.get("/", headers=headers)).status_code == 200
        assert (await client.get("/api/state", headers=headers)).status_code == 400
        assert (
            await client.post("/api/disconnect", json={}, headers=headers)
        ).status_code == 400
        assert (await capability.provider.sessions.status()).state == "authorized"
        assert len(api.calls) == 1


async def test_repeated_abandoned_reconnects_do_not_exhaust_sign_in(
    tmp_path: Path,
) -> None:
    capability, _, _, api, _ = await prepared(tmp_path)
    async with browser(ManagementServer(capability)) as client:
        urls: set[str] = set()
        for _ in range(8):
            result = await client.post("/api/connect", json={})
            assert result.status_code == 200
            url = result.json()["launch_url"]
            assert url not in urls
            urls.add(url)
            assert (await capability.provider.sessions.status()).state == "authorized"
        assert len(api.calls) == 1


async def test_previous_browser_cleanup_cannot_cancel_a_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability, _, catalog, _, _ = await prepared(tmp_path)
    joining = asyncio.Event()
    release_cleanup = asyncio.Event()
    delayed = False
    read = asyncio.StreamReader.read

    async def delayed_read(reader: asyncio.StreamReader, n: int = -1) -> bytes:
        nonlocal delayed
        try:
            return await read(reader, n)
        except asyncio.CancelledError:
            # Delay the old HTTP connection's cleanup after its generation
            # completed, while a second real HTTP request starts streaming.
            if n == 1 and not delayed:
                delayed = True
                joining.set()
                await release_cleanup.wait()
            raise

    monkeypatch.setattr(asyncio.StreamReader, "read", delayed_read)
    second_stream = SlowText()
    starts = 0

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal starts
        starts += 1
        if starts == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=event_bytes({"type": "response.completed"}),
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=second_stream
        )

    capability.provider.transport = httpx.MockTransport(respond)
    async with browser(ManagementServer(capability)) as client:
        first = asyncio.create_task(
            client.post("/api/generate", json=payload(catalog.revision))
        )
        second: asyncio.Task[httpx.Response] | None = None
        try:
            await asyncio.wait_for(joining.wait(), 2)
            second = asyncio.create_task(
                client.post("/api/generate", json=payload(catalog.revision))
            )
            await asyncio.wait_for(second_stream.entered.wait(), 2)
            release_cleanup.set()
            assert '"kind":"completed"' in (await asyncio.wait_for(first, 2)).text
            assert (await client.get("/api/state")).json()["busy"]
            second_stream.release.set()
            response = await asyncio.wait_for(second, 2)
            assert '"kind":"completed"' in response.text
            assert '"kind":"failed"' not in response.text
            assert starts == 2
        finally:
            release_cleanup.set()
            second_stream.release.set()
            await asyncio.gather(
                first, *(() if second is None else (second,)), return_exceptions=True
            )


@pytest.mark.parametrize(
    "headers",
    [
        "Content-Length: -1\r\n",
        "Content-Length: 999999\r\n",
        "Content-Length: 0\r\nContent-Length: 0\r\n",
        "Transfer-Encoding: chunked\r\n",
    ],
)
async def test_malformed_http_is_bounded_and_cannot_mutate(
    tmp_path: Path, headers: str
) -> None:
    capability, _, _, api, _ = await prepared(tmp_path)
    server = ManagementServer(capability)
    async with browser(server):
        port = int(server.url.rsplit(":", 1)[1])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            (
                f"POST /api/disconnect HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                + headers
                + "\r\n"
            ).encode()
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 2)
        assert response.startswith(b"HTTP/1.1 400")
        writer.close()
        await writer.wait_closed()
        assert (await capability.provider.sessions.status()).state == "authorized"
        assert len(api.calls) == 1
