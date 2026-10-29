"""Owner-local key export and account-independent signing for one delegated call."""

import argparse
import json
import os
import secrets
import stat
import sys
import time
from pathlib import Path

from skulk_inference_reference.contracts import GenerationRequest, Requirements

from .admission import CallerCredential, Invocation
from .credentials import CredentialStore


def write_credential(store: CredentialStore, credential: CallerCredential) -> Path:
    """Export caller-only signing material once, beneath the protected state root.

    The returned path is safe to print; file contents are secret and must never
    become diagnostics, settings or a provider invocation payload.
    """
    path = store.root / f"caller-{credential.grant.grant_id}.json"
    encoded = json.dumps(
        {
            "grant": credential.grant.model_dump(mode="json"),
            "private_key": credential.private_key.get_secret_value(),
        },
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def read_credential(path: Path) -> CallerCredential:
    """Read a bounded private regular file; never accept links or loose modes."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError("caller credential must be owner-only")
        raw = source.read(16385)
    if len(raw) > 16384:
        raise ValueError("caller credential exceeds size bound")
    return CallerCredential.model_validate_json(raw)


def main() -> None:
    """Sign one bounded stdin request; stdout is a confidential invocation payload."""
    parser = argparse.ArgumentParser(description="Sign one delegated inference request")
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--target-node", required=True)
    parser.add_argument("--callable-id", default="openai.inference.generate")
    parser.add_argument("--descriptor-revision", required=True)
    parser.add_argument("--catalog-revision", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--allow-external-unknown-usage", action="store_true", required=True
    )
    arguments = parser.parse_args()
    try:
        credential = read_credential(Path(str(arguments.credential_file)))
        now = time.time()
        if credential.grant.expires_at <= now:
            raise ValueError("expired caller grant")
        raw = sys.stdin.buffer.read(32769)
        if len(raw) > 32768:
            raise ValueError("oversized request")
        request = GenerationRequest.model_validate_json(raw)
        invocation = Invocation(
            target_node=str(arguments.target_node),
            callable_id=str(arguments.callable_id),
            descriptor_revision=str(arguments.descriptor_revision),
            catalog_revision=str(arguments.catalog_revision),
            model=str(arguments.model),
            request=request,
            requirements=Requirements(
                allowed_domains=frozenset({"external"}),
                allowed_usage=frozenset({"unknown"}),
                minimum_context_tokens=None,
            ),
            nonce=secrets.token_hex(32),
            expires_at=min(now + 30, credential.grant.expires_at),
        )
        print(credential.sign(invocation).model_dump_json())
    except (OSError, ValueError):
        print("caller_signing_failed", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
