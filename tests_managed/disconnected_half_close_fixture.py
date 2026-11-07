"""Reproduce a peer disappearing immediately before the media half-close."""

import argparse
import asyncio
import errno
import json
from pathlib import Path

from skulk_capability_sdk.storage import read_startup

from skulk_openai import managed


def main() -> None:
    """Fault one mock stream after durable commit, with no real account access."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-fd", type=int, required=True)
    options = parser.parse_args()
    startup = read_startup(options.config_fd)
    configuration = managed.Configuration.model_validate_json(
        json.dumps(startup.configuration or {})
    )
    if (
        startup.manifest.bundle_id != "reference.openai.mock"
        or configuration.state_directory is None
    ):
        raise ValueError("half-close probe requires explicit isolated mock storage")
    marker = Path(configuration.state_directory) / "eof-probe"
    original = asyncio.StreamWriter.write_eof

    def disconnected(self: asyncio.StreamWriter) -> None:
        if not marker.exists():
            marker.write_text("ENOTCONN")
            raise OSError(errno.ENOTCONN, "synthetic peer already disconnected")
        original(self)

    asyncio.StreamWriter.write_eof = disconnected
    asyncio.run(managed.serve(startup))


if __name__ == "__main__":
    main()
