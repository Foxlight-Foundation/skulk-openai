"""Explicit standalone launch of the capability-owned, loopback-only UI."""

import argparse
import asyncio
import contextlib
import signal
import sys
import webbrowser
from pathlib import Path

from .capability import InferenceCapability
from .credentials import CredentialStore, default_state_directory
from .management import ManagementServer
from .oauth import OAuthClient
from .provider import ProviderClient
from .sessions import SessionManager


async def run(
    *,
    state_directory: Path,
    port: int = 0,
    enable_inference: bool = False,
    open_browser: bool = True,
) -> None:
    """Serve until interrupted; loading the page performs no provider request."""
    sessions = SessionManager(
        CredentialStore(state_directory), OAuthClient(allow_live=True)
    )
    capability = InferenceCapability(
        ProviderClient(sessions, allow_live=True),
        target_node="owner-local",
        callable_id="openai.owner.test",
        descriptor_revision="0000000000000001",
        enabled=enable_inference,
    )
    server = ManagementServer(capability)
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, done.set)
    try:
        url = await server.start(port=port)
        print("Skulk OpenAI Integrator: " + url, flush=True)
        if open_browser:
            await asyncio.to_thread(webbrowser.open, url)
        await done.wait()
    finally:
        await server.close()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def main() -> None:
    """Launch owner management; explicit send/connect actions authorize effects."""
    parser = argparse.ArgumentParser(description="Skulk OpenAI Integrator")
    parser.add_argument("--state-dir", type=Path, default=default_state_directory())
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--enable-inference", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    arguments = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        try:
            asyncio.run(
                run(
                    state_directory=arguments.state_dir,
                    port=arguments.port,
                    enable_inference=arguments.enable_inference,
                    open_browser=not arguments.no_browser,
                )
            )
        except (OSError, ValueError):
            print(
                "Management could not start. Check the state directory and port.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
