"""Explicit owner-local setup; it never exposes authentication as a fabric call."""

import argparse
import asyncio
import contextlib
import secrets
import sqlite3
import sys
import webbrowser
from contextlib import aclosing
from pathlib import Path
from urllib.parse import urlsplit

from skulk_inference_reference.contracts import GenerationRequest, Message

from .admission import GrantStore
from .auth_contracts import AuthFailure, ConnectionStatus
from .caller import write_credential
from .credentials import CredentialStore, default_state_directory
from .oauth import AuthorizationAttempt, OAuthClient
from .provider import CatalogCache, ProviderClient, ProviderFailure
from .sessions import SessionManager


class CallbackListener:
    """A bounded IPv4 loopback listener with an unguessable browser launch path."""

    def __init__(self, manager: SessionManager) -> None:
        """Bind one owner-local session manager, without opening any socket yet."""
        self.manager = manager
        self.server: asyncio.Server | None = None
        self.attempt: AuthorizationAttempt | None = None
        self.redirect_uri = ""
        self.launch_url = ""
        self._launch_path = "/continue/" + secrets.token_urlsafe(32)
        self._future: asyncio.Future[ConnectionStatus] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._connections = 0
        self._launched = False
        self._callback_lock = asyncio.Lock()

    async def start(self, *, registration_id: str | None = None) -> None:
        """Start the listener before creating an authorization request."""
        self._future = asyncio.get_running_loop().create_future()
        self.server = await asyncio.start_server(
            self._accept, host="127.0.0.1", port=0, limit=16384
        )
        port = self.server.sockets[0].getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        self.redirect_uri = origin + "/auth/callback"
        self.launch_url = origin + self._launch_path
        try:
            self.attempt = await self.manager.begin(
                self.redirect_uri, registration_id=registration_id
            )
        except BaseException:
            await self.close()
            raise

    def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._connections += 1
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            if self._connections > 32 or len(self._tasks) > 4:
                return
            async with asyncio.timeout(5):
                raw = await reader.readuntil(b"\r\n\r\n")
                if len(raw) > 16384:
                    return
                lines = raw.decode("ascii").split("\r\n")
                method, target, version = lines[0].split(" ")
                headers: dict[str, str] = {}
                for line in lines[1:-2]:
                    name, value = line.split(":", 1)
                    name = name.strip().lower()
                    if name in headers:
                        return
                    headers[name] = value.strip()
                if (
                    method != "GET"
                    or version != "HTTP/1.1"
                    or headers.get("host") != urlsplit(self.redirect_uri).netloc
                    or "transfer-encoding" in headers
                    or headers.get("content-length", "0") != "0"
                    or not target.startswith("/")
                    or target.startswith("//")
                ):
                    return
            async with asyncio.timeout(40):
                location = urlsplit(target)
                attempt, future = self.attempt, self._future
                if attempt is None or future is None or future.done():
                    await self._reply(writer, "410 Gone", "This attempt is finished.")
                elif target == self._launch_path and not self._launched:
                    self._launched = True
                    await self._reply(
                        writer,
                        "302 Found",
                        "Continue with ChatGPT in your browser.",
                        redirect=attempt.url.get_secret_value(),
                    )
                elif location.path == "/auth/callback" and not location.fragment:
                    async with self._callback_lock:
                        if future.done():
                            await self._reply(writer, "410 Gone", "Attempt finished.")
                            return
                        try:
                            status = await self.manager.complete(
                                attempt, location.query
                            )
                        except AuthFailure as error:
                            if not self.manager.client.is_pending(attempt):
                                future.set_exception(error)
                            await self._reply(
                                writer,
                                "400 Bad Request",
                                "Connection was not completed.",
                            )
                        except (OSError, ValueError, sqlite3.Error, TimeoutError):
                            future.set_exception(AuthFailure("credentials_unavailable"))
                            await self._reply(
                                writer, "500 Internal Server Error", "Setup failed."
                            )
                        else:
                            future.set_result(status)
                            await self._reply(
                                writer,
                                "200 OK",
                                "Connection saved. You may close this tab.",
                            )
                else:
                    await self._reply(writer, "404 Not Found", "Unknown setup path.")
        except (
            OSError,
            ValueError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
        ):
            return
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    @staticmethod
    async def _reply(
        writer: asyncio.StreamWriter,
        status: str,
        body: str,
        *,
        redirect: str | None = None,
    ) -> None:
        encoded = body.encode("utf-8")
        headers = (
            f"HTTP/1.1 {status}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Cache-Control: no-store\r\n"
            "Referrer-Policy: no-referrer\r\n"
            "Content-Security-Policy: default-src 'none'; frame-ancestors 'none'\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "Connection: close\r\n"
            f"Content-Length: {len(encoded)}\r\n"
        )
        if redirect is not None:
            headers += f"Location: {redirect}\r\n"
        writer.write(headers.encode("ascii") + b"\r\n" + encoded)
        await writer.drain()

    async def wait(self, *, deadline_seconds: float = 600) -> ConnectionStatus:
        """Wait for one validated callback, closing all owned sockets on exit."""
        if self._future is None:
            raise ValueError("listener has not started")
        try:
            async with asyncio.timeout(deadline_seconds):
                return await self._future
        finally:
            await self.close()

    async def close(self) -> None:
        """Close the listener and join or cancel every accepted connection task."""
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        if self._future is not None:
            if self._future.done() and not self._future.cancelled():
                self._future.exception()
            else:
                self._future.cancel()
        tasks = tuple(self._tasks)
        # Give the successful callback time to flush its response before closing.
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=0.2)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def run(arguments: argparse.Namespace) -> int:
    """Perform one explicit operator command; status never contacts the issuer."""
    store = CredentialStore(Path(str(arguments.state_dir)))
    manager = SessionManager(store, OAuthClient(allow_live=True))
    command = str(arguments.command)
    if command == "status":
        print((await manager.status()).model_dump_json())
        return 0
    if command == "disconnect":
        confirmed = await manager.disconnect()
        print("Disconnected locally.")
        if not confirmed:
            print(
                "Remote revocation was not confirmed. Disconnect this app in "
                "ChatGPT Settings if necessary."
            )
        return 0 if confirmed else 2
    if command == "revoke-grant":
        await asyncio.to_thread(GrantStore(store).revoke, str(arguments.grant_id))
        print("Caller grant revoked for new admission.")
        return 0
    if command == "issue-grant":
        provider = ProviderClient(manager, allow_live=True)
        catalog = await provider.catalog()
        await asyncio.to_thread(CatalogCache(manager).save, catalog)
        grants = GrantStore(store)
        credential = await asyncio.to_thread(
            grants.issue,
            catalog,
            models=tuple(str(model) for model in arguments.model),
            maximum_attempts=int(arguments.maximum_attempts),
            maximum_output_characters=int(arguments.maximum_output_characters),
            lifetime_seconds=float(arguments.lifetime_seconds),
            allow_external_unknown_usage=bool(arguments.allow_external_unknown_usage),
        )
        try:
            path = await asyncio.to_thread(write_credential, store, credential)
        except OSError:
            await asyncio.to_thread(grants.revoke, credential.grant.grant_id)
            raise
        print("Caller credential saved: " + str(path))
        print("Grant ID: " + credential.grant.grant_id)
        return 0
    if command in {"models", "probe"}:
        provider = ProviderClient(manager, allow_live=True)
        catalog = await provider.catalog()
        await asyncio.to_thread(CatalogCache(manager).save, catalog)
        if command == "models":
            print(catalog.model_dump_json())
            return 0
        raw = await asyncio.to_thread(sys.stdin.buffer.read, 4097)
        text = raw.decode("utf-8").strip()
        if not text or len(raw) > 4096:
            raise ValueError("probe input must be bounded UTF-8 text")
        request = GenerationRequest(messages=(Message(role="user", content=text),))
        async with aclosing(
            provider.generate(request, catalog=catalog, model=str(arguments.model))
        ) as events:
            async for event in events:
                if event.kind == "text":
                    print(event.text or "", end="", flush=True)
                elif event.kind == "failed":
                    print("\n" + (event.error or "upstream_failed"), file=sys.stderr)
                    return 1
        print()
        return 0
    selected = None if arguments.new_account else await asyncio.to_thread(store.load)
    registration_id = str(arguments.registration) if arguments.registration else None
    if registration_id is None and selected is not None:
        registration_id = selected.registration_id
    listener = CallbackListener(manager)
    await listener.start(registration_id=registration_id)
    print("Continue with ChatGPT: " + listener.launch_url, flush=True)
    if not arguments.no_browser:
        await asyncio.to_thread(webbrowser.open, listener.launch_url)
    status = await listener.wait()
    print(status.model_dump_json())
    if status.state == "identity_only":
        print("Signed in; ChatGPT plan usage permission was not granted.")
    return 0


def main() -> None:
    """Run setup explicitly; never print provider errors, credentials or ID hints."""
    parser = argparse.ArgumentParser(
        description="Owner-local OpenAI capability account"
    )
    parser.add_argument("--state-dir", type=Path, default=default_state_directory())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("disconnect")
    commands.add_parser("models")
    grant = commands.add_parser("issue-grant")
    grant.add_argument("--model", action="append", required=True)
    grant.add_argument("--maximum-attempts", type=int, default=1)
    grant.add_argument("--maximum-output-characters", type=int, default=1024)
    grant.add_argument("--lifetime-seconds", type=float, default=300)
    grant.add_argument(
        "--allow-external-unknown-usage", action="store_true", required=True
    )
    revoke = commands.add_parser("revoke-grant")
    revoke.add_argument("--grant-id", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("--model", required=True)
    probe.add_argument("--allow-unknown-usage", action="store_true", required=True)
    connect = commands.add_parser("connect")
    selection = connect.add_mutually_exclusive_group()
    selection.add_argument("--new-account", action="store_true")
    selection.add_argument("--registration")
    connect.add_argument("--no-browser", action="store_true")
    arguments = parser.parse_args()
    try:
        result = asyncio.run(run(arguments))
    except AuthFailure as error:
        print(error.code, file=sys.stderr)
        result = 1
    except ProviderFailure as error:
        print(error.code, file=sys.stderr)
        result = 1
    except (OSError, ValueError, sqlite3.Error, TimeoutError):
        print("account_setup_failed", file=sys.stderr)
        result = 1
    except KeyboardInterrupt:
        result = 130
    raise SystemExit(result)


if __name__ == "__main__":
    main()
