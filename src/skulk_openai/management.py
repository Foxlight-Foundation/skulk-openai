"""Owner-local web management, separate from fabric callers and account secrets."""

import asyncio
import contextlib
import json
import secrets
import sqlite3
from contextlib import aclosing
from dataclasses import dataclass
from importlib.resources import files
from typing import Literal

import httpx
from pydantic import Field, JsonValue, TypeAdapter

from skulk_inference_reference.contracts import (
    FrozenContract,
    GenerationEvent,
    GenerationRequest,
    Identifier,
    Message,
    Requirements,
)

from .admission import CallerCredential, Invocation
from .auth_contracts import AuthFailure
from .capability import InferenceCapability
from .provider import ProviderFailure
from .settings import IntegrationSettings, account_binding
from .setup import CallbackListener

_OBJECT = TypeAdapter(dict[str, JsonValue])
_ASSETS = {
    "/": ("index.html", "text/html"),
    "/assets/app.css": ("app.css", "text/css"),
    "/assets/app.js": ("app.js", "text/javascript"),
    "/assets/skulk.svg": ("skulk.svg", "image/svg+xml"),
    "/assets/instrument-sans.woff2": ("instrument-sans.woff2", "font/woff2"),
    "/assets/jetbrains-mono.woff2": ("jetbrains-mono.woff2", "font/woff2"),
}
_FORWARDED = {
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
}


class TestRequest(FrozenContract):
    """One explicit owner action; policy choices never become persistent grants."""

    model: Identifier
    prompt: str = Field(min_length=1, max_length=4096)
    catalog_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    processing: Literal["local_only", "allow_openai"]
    usage: Literal["included_only", "app_settings"]


class ConnectRequest(FrozenContract):
    """An explicit request to start consent, optionally recovering plan permission."""

    request_plan_consent: bool = False


class GatewayLookup(FrozenContract):
    """Fetch only a public gateway key from a fixed loopback API path."""

    port: int = Field(ge=1024, le=65535)


@dataclass(frozen=True)
class _Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class ManagementServer:
    """Serve a loopback-only management surface owned by one capability process.

    The page bootstraps a host-local HttpOnly session. Mutations additionally
    require the exact browser Origin and a separate CSRF header. Neither token
    appears in a surface URL, account record, descriptor or browser storage.
    This trusts the host user and browser; it is not remote operator delegation.
    """

    def __init__(self, capability: InferenceCapability) -> None:
        """Reuse the capability's real session, catalog and admission boundaries."""
        self.capability = capability
        self.sessions = capability.provider.sessions
        self.server: asyncio.Server | None = None
        self.url = ""
        self._session = secrets.token_hex(32)
        self._csrf = secrets.token_hex(32)
        self._cookie = ""
        self._tasks: set[asyncio.Task[None]] = set()
        self._generation: asyncio.Task[None] | None = None
        self._oauth: CallbackListener | None = None
        self._consent: asyncio.Task[None] | None = None
        self._operation = asyncio.Lock()
        self._notice: str | None = None

    async def start(self, *, port: int = 0) -> str:
        """Bind IPv4 loopback and return a credential-free surface URL."""
        if self.server is not None or (port != 0 and not 1024 <= port <= 65535):
            raise ValueError("invalid management listener")
        self.server = await asyncio.start_server(
            self._accept, "127.0.0.1", port, limit=16384, backlog=16
        )
        actual = self.server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{actual}"
        self._cookie = f"skulk_openai_{actual}"
        return self.url + "/"

    def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if len(self._tasks) >= 16:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read(self, reader: asyncio.StreamReader) -> _Request:
        async with asyncio.timeout(5):
            head = await reader.readuntil(b"\r\n\r\n")
            if len(head) > 16384:
                raise ValueError("headers too large")
            lines = head.decode("ascii").split("\r\n")
            method, path, version = lines[0].split(" ")
            headers: dict[str, str] = {}
            for line in lines[1:-2]:
                name, value = line.split(":", 1)
                name = name.lower()
                if not name or name != name.strip() or name in headers:
                    raise ValueError("invalid header")
                headers[name] = value.strip()
            length = headers.get("content-length", "0")
            if (
                version != "HTTP/1.1"
                or not length.isascii()
                or not length.isdecimal()
                or int(length) > 16384
                or "transfer-encoding" in headers
            ):
                raise ValueError("invalid framing")
            # Drain only a validated bounded body before refusing its origin.
            # Closing with unread TCP bytes can reset the connection and hide
            # an otherwise complete refusal response from the browser.
            body = await reader.readexactly(int(length))
            if (
                headers.get("host") != self.url.removeprefix("http://")
                or _FORWARDED.intersection(headers)
                or (
                    headers.get("sec-fetch-site") == "cross-site"
                    and not (
                        method == "GET"
                        and path == "/"
                        and headers.get("sec-fetch-mode") == "navigate"
                        and headers.get("sec-fetch-dest") == "document"
                    )
                )
                or ("origin" in headers and headers["origin"] != self.url)
                or not path.startswith("/")
                or "?" in path
                or "#" in path
            ):
                raise ValueError("invalid request")
            return _Request(method, path, headers, body)

    def _authorized(self, request: _Request) -> bool:
        cookies = [
            value.strip() for value in request.headers.get("cookie", "").split(";")
        ]
        return any(
            secrets.compare_digest(value, f"{self._cookie}={self._session}")
            for value in cookies
        )

    async def _headers(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        media: str,
        *,
        length: int | None,
        cookie: bool = False,
    ) -> None:
        reasons = {
            200: "OK",
            400: "Bad Request",
            403: "Forbidden",
            404: "Not Found",
            409: "Conflict",
            503: "Service Unavailable",
        }
        headers = (
            f"HTTP/1.1 {status} {reasons[status]}\r\n"
            f"Content-Type: {media}; charset=utf-8\r\n"
            "Cache-Control: no-store\r\nReferrer-Policy: no-referrer\r\n"
            "X-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\n"
            "Content-Security-Policy: default-src 'none'; script-src 'self'; "
            "style-src 'self'; font-src 'self'; img-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'\r\n"
            "Connection: close\r\n"
        )
        if length is not None:
            headers += f"Content-Length: {length}\r\n"
        if cookie:
            headers += (
                f"Set-Cookie: {self._cookie}={self._session}; "
                "HttpOnly; SameSite=Strict; Path=/\r\n"
            )
        writer.write((headers + "\r\n").encode("ascii"))
        await writer.drain()

    async def _json(
        self,
        writer: asyncio.StreamWriter,
        value: dict[str, JsonValue],
        *,
        status: int = 200,
    ) -> None:
        body = json.dumps(value, allow_nan=False).encode()
        await self._headers(writer, status, "application/json", length=len(body))
        writer.write(body)
        await writer.drain()

    async def _state(self) -> dict[str, JsonValue]:
        status = await self.sessions.status()
        catalog = await asyncio.to_thread(self.capability.cache.read)
        fresh = (
            catalog is not None
            and catalog.expires_at > self.capability.clock()
            and status.state == "authorized"
            and status.registration_id == catalog.registration_id
            and status.generation == catalog.generation
        )
        return {
            "connection": status.state,
            "inference_enabled": self.capability.enabled,
            "models": [
                {"slug": m.slug, "title": m.display_name} for m in catalog.models
            ]
            if catalog is not None and fresh
            else [],
            "catalog_revision": catalog.revision
            if catalog is not None and fresh
            else None,
            "catalog_fresh": fresh,
            "cached_models": [
                {"slug": m.slug, "title": m.display_name} for m in catalog.models
            ]
            if catalog is not None
            and status.registration_id == catalog.registration_id
            and status.generation == catalog.generation
            else [],
            "busy": self._generation is not None and not self._generation.done(),
            "connecting": self._consent is not None and not self._consent.done(),
            "notice": self._notice,
            "csrf": self._csrf,
            "settings": _OBJECT.validate_json(
                self.capability.settings.read().model_dump_json(
                    exclude={"account_binding"}
                )
            ),
            "gateway_delegation_current": (
                status.registration_id is not None
                and status.generation is not None
                and self.capability.settings.read().account_binding
                == account_binding(status.registration_id, status.generation)
            ),
        }

    async def _watch_consent(self, listener: CallbackListener) -> None:
        try:
            status = await listener.wait()
            self._notice = (
                "permission_missing" if status.state == "identity_only" else "connected"
            )
        except (AuthFailure, OSError, ValueError, sqlite3.Error, TimeoutError):
            self._notice = "connection_failed"
        finally:
            await listener.close()
            if self._oauth is listener:
                self._oauth = None

    async def _stop_consent(self) -> None:
        if self._consent is not None:
            self._consent.cancel()
            await asyncio.gather(self._consent, return_exceptions=True)
            self._consent = None
        if self._oauth is not None:
            await self._oauth.close()
            self._oauth = None

    async def _stop_generation(self) -> None:
        if self._generation is not None and not self._generation.done():
            if not self._generation.cancelling():
                self._generation.cancel()
            await asyncio.gather(self._generation, return_exceptions=True)

    async def _issue(self, model: str) -> CallerCredential:
        catalog = await asyncio.to_thread(self.capability.cache.read)
        if catalog is None:
            raise AuthFailure("permission_missing")
        task = asyncio.create_task(
            asyncio.to_thread(
                self.capability.grants.issue,
                catalog,
                models=(model,),
                maximum_attempts=1,
                maximum_output_characters=min(
                    1024, self.capability.settings.read().maximum_output_characters
                ),
                lifetime_seconds=60.0,
                allow_external_unknown_usage=True,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A thread can commit after cancellation. Recover its exact id before
            # returning so the browser never leaves an unrevoked delegation.
            credential = await task
            await asyncio.to_thread(
                self.capability.grants.revoke, credential.grant.grant_id
            )
            raise

    async def _stream(self, writer: asyncio.StreamWriter, request: TestRequest) -> None:
        credential: CallerCredential | None = None

        async def emit(event: GenerationEvent) -> None:
            writer.write(event.model_dump_json().encode() + b"\n")
            await asyncio.wait_for(writer.drain(), 5)

        try:
            async with asyncio.timeout(30):
                credential = await self._issue(request.model)
                invocation = Invocation(
                    target_node=self.capability.target_node,
                    callable_id=self.capability.callable_id,
                    descriptor_revision=self.capability.descriptor_revision,
                    catalog_revision=request.catalog_revision,
                    model=request.model,
                    request=GenerationRequest(
                        messages=(Message(role="user", content=request.prompt),),
                        maximum_output_characters=min(
                            1024,
                            self.capability.settings.read().maximum_output_characters,
                        ),
                    ),
                    requirements=Requirements(
                        allowed_domains=frozenset({"external"}),
                        allowed_usage=frozenset({"unknown"}),
                        minimum_context_tokens=None,
                    ),
                    nonce=secrets.token_hex(32),
                    expires_at=self.capability.clock() + 30,
                )
                async with aclosing(
                    self.capability.generate(credential.sign(invocation))
                ) as events:
                    async for event in events:
                        await emit(event)
        except asyncio.CancelledError:
            with contextlib.suppress(ConnectionError, TimeoutError):
                await emit(GenerationEvent(kind="failed", error="interrupted"))
        except (AuthFailure, ProviderFailure):
            with contextlib.suppress(ConnectionError, TimeoutError):
                await emit(GenerationEvent(kind="failed", error="policy_denied"))
        except (OSError, ValueError, sqlite3.Error, TimeoutError):
            with contextlib.suppress(ConnectionError, TimeoutError):
                await emit(GenerationEvent(kind="failed", error="upstream_failed"))
        finally:
            if credential is not None:
                revoke = asyncio.create_task(
                    asyncio.to_thread(
                        self.capability.grants.revoke, credential.grant.grant_id
                    )
                )
                try:
                    await asyncio.shield(revoke)
                except asyncio.CancelledError:
                    await revoke
                    raise

    async def _post(
        self,
        request: _Request,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if request.path == "/api/notice":
            if _OBJECT.validate_json(request.body) != {}:
                raise ValueError("unexpected notice fields")
            async with self._operation:
                settings = await asyncio.to_thread(self.capability.settings.read)
                await asyncio.to_thread(
                    self.capability.settings.save,
                    settings.model_copy(update={"plan_notice_seen": True}),
                )
            await self._json(writer, {"acknowledged": True})
            return
        if request.path == "/api/gateway":
            lookup = GatewayLookup.model_validate_json(request.body)
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=3
            ) as client:
                async with client.stream(
                    "GET", f"http://127.0.0.1:{lookup.port}/v1/inference/gateway"
                ) as response:
                    if response.status_code != 200:
                        raise ValueError("gateway unavailable")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 4096:
                            raise ValueError("invalid gateway")
            value = _OBJECT.validate_json(raw)
            key = value.get("public_key")
            if (
                value.get("contract") != "chat.completions@0.1.0"
                or not isinstance(key, str)
                or len(key) != 64
                or any(c not in "0123456789abcdef" for c in key)
            ):
                raise ValueError("invalid gateway key")
            await self._json(writer, {"public_key": key})
            return
        if request.path == "/api/settings":
            value = _OBJECT.validate_json(request.body)
            # Account fencing is derived from our protected store, never from
            # a browser-supplied registration id or generation claim.
            if "account_binding" in value:
                raise ValueError("reserved setting")
            settings = IntegrationSettings.model_validate_json(json.dumps(value))
            previous = await asyncio.to_thread(self.capability.settings.read)
            status = await self.sessions.status()
            if settings.gateway_public_key is not None:
                if (
                    status.state == "authorized"
                    and status.registration_id is not None
                    and status.generation is not None
                ):
                    binding = account_binding(status.registration_id, status.generation)
                elif (
                    not settings.inference_enabled
                    and settings.gateway_public_key == previous.gateway_public_key
                ):
                    # Disabling must work offline; retaining an inactive old
                    # fence does not approve a new account or gateway key.
                    binding = previous.account_binding
                else:
                    raise ValueError("connect account before gateway delegation")
                settings = settings.model_copy(update={"account_binding": binding})
            catalog = await asyncio.to_thread(self.capability.cache.read)
            revoking = not settings.inference_enabled or (
                previous.gateway_public_key is not None
                and settings.gateway_public_key is None
            )
            if (
                settings.models
                and (
                    catalog is None
                    or not set(settings.models).issubset(m.slug for m in catalog.models)
                )
                and not (revoking and set(settings.models).issubset(previous.models))
            ):
                raise ValueError("model is absent from account catalog")
            async with self._operation:
                await self._stop_generation()
                await asyncio.to_thread(self.capability.settings.save, settings)
                self.capability.enabled = settings.inference_enabled
            await self._json(writer, await self._state())
            return
        if request.path == "/api/generate":
            payload = TestRequest.model_validate_json(request.body)
            settings = self.capability.settings.read()
            # Hard policy precedes grant creation and every provider request,
            # including catalog refresh; a local-only click sends nothing out.
            if (
                payload.processing != "allow_openai"
                or payload.usage != "app_settings"
                or not self.capability.enabled
                or (
                    self.capability.settings.configured()
                    and (
                        not settings.inference_enabled
                        or settings.processing != "allow_openai"
                        or settings.usage != "app_settings"
                        or payload.model not in settings.models
                    )
                )
            ):
                await self._json(writer, {"error": "policy_denied"}, status=403)
                return
            if self._generation is not None and not self._generation.done():
                await self._json(writer, {"error": "busy"}, status=409)
                return
            await self._headers(writer, 200, "application/x-ndjson", length=None)
            generation = asyncio.create_task(self._stream(writer, payload))
            self._generation = generation
            disconnected = asyncio.create_task(reader.read(1))
            try:
                done, _ = await asyncio.wait(
                    (generation, disconnected),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnected in done:
                    generation.cancel()
                await generation
            finally:
                disconnected.cancel()
                await asyncio.gather(disconnected, return_exceptions=True)
                # A new browser call may start as soon as this generation is
                # done. This handler must never await or cancel its successor.
                if not generation.done():
                    if not generation.cancelling():
                        generation.cancel()
                    await asyncio.gather(generation, return_exceptions=True)
                if self._generation is generation:
                    self._generation = None
            return
        if request.path == "/api/connect":
            payload = ConnectRequest.model_validate_json(request.body)
        else:
            if _OBJECT.validate_json(request.body) != {}:
                raise ValueError("unexpected input")
            payload = ConnectRequest()
        if request.path == "/api/cancel":
            await self._stop_generation()
            await self._json(writer, {"cancelled": True})
            return
        if request.path not in {"/api/connect", "/api/disconnect", "/api/models"}:
            await self._json(writer, {"error": "not_found"}, status=404)
            return
        if self._operation.locked():
            await self._json(writer, {"error": "busy"}, status=409)
            return
        async with self._operation:
            if request.path == "/api/connect":
                await self._stop_consent()
                listener = CallbackListener(self.sessions)
                selected = await asyncio.to_thread(self.sessions.store.load)
                await listener.start(
                    registration_id=selected.registration_id if selected else None,
                    request_plan_consent=payload.request_plan_consent,
                )
                self._oauth = listener
                self._notice = None
                self._consent = asyncio.create_task(self._watch_consent(listener))
                await self._json(writer, {"launch_url": listener.launch_url})
            elif request.path == "/api/disconnect":
                await self._stop_consent()
                await self._stop_generation()
                confirmed = await self.sessions.disconnect()
                self._notice = "disconnected" if confirmed else "revocation_unconfirmed"
                await self._json(writer, {"revocation_confirmed": confirmed})
            else:
                catalog = await self.capability.provider.catalog()
                await asyncio.to_thread(self.capability.cache.save, catalog)
                await self._json(writer, await self._state())

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            async with asyncio.timeout(45):
                request = await self._read(reader)
                if (
                    request.method == "GET"
                    and not request.body
                    and request.path in _ASSETS
                ):
                    name, media = _ASSETS[request.path]
                    body = files("skulk_openai").joinpath("web", name).read_bytes()
                    await self._headers(
                        writer, 200, media, length=len(body), cookie=request.path == "/"
                    )
                    writer.write(body)
                    await writer.drain()
                elif not self._authorized(request):
                    await self._json(
                        writer, {"error": "owner_session_required"}, status=403
                    )
                elif (
                    request.method == "GET"
                    and not request.body
                    and request.path == "/api/state"
                ):
                    await self._json(writer, await self._state())
                elif request.method == "POST":
                    if (
                        request.headers.get("origin") != self.url
                        or request.headers.get("content-type") != "application/json"
                        or not secrets.compare_digest(
                            request.headers.get("x-csrf-token", ""), self._csrf
                        )
                    ):
                        await self._json(
                            writer, {"error": "owner_action_required"}, status=403
                        )
                    else:
                        await self._post(request, reader, writer)
                else:
                    await self._json(writer, {"error": "not_found"}, status=404)
        except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            with contextlib.suppress(ConnectionError):
                await self._json(writer, {"error": "invalid_request"}, status=400)
        except (
            AuthFailure,
            ProviderFailure,
            httpx.HTTPError,
            OSError,
            sqlite3.Error,
            TimeoutError,
        ):
            with contextlib.suppress(ConnectionError):
                await self._json(writer, {"error": "operation_failed"}, status=503)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def close(self) -> None:
        """Withdraw the surface, cancel calls/sign-in and join every owned task."""
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        await self._stop_consent()
        await self._stop_generation()
        pending = tuple(self._tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.server = None
        self.url = ""
