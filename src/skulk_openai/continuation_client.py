"""Account-free client key generation and exact-body continuation signing."""

import argparse
import json
import os
import secrets
import stat
import sys
import time
from pathlib import Path

from skulk_inference_reference.continuation import ContinuationRequest
from skulk_inference_reference.continuation_auth import (
    ClientEnrollment,
    ContinuationKey,
    ReceiptQuery,
)
from skulk_inference_reference.input_body import MAX_BODY_BYTES, InputBody


def write_key(path: Path, key: ContinuationKey) -> None:
    """Write caller-only private material once, refusing replacement and links."""
    raw = json.dumps({"private_key": key.private_key.get_secret_value()}).encode()
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _read(path: Path, *, private: bool) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or (
            private
            and (
                metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            )
        ):
            raise ValueError("invalid client record permissions or type")
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError("client record exceeds bound")
    return raw


def read_key(path: Path) -> ContinuationKey:
    """Read a bounded owner-only client key, never an account credential."""
    return ContinuationKey.model_validate_json(_read(path, private=True))


def read_enrollment(path: Path) -> ClientEnrollment:
    """Read public owner approval; binding independently verifies its public key."""
    return ClientEnrollment.model_validate_json(_read(path, private=False))


def main() -> None:
    """Print public enrollment material or a proof; never print the private key."""
    parser = argparse.ArgumentParser(
        description="Offline continuation client keys and signing"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    generate = subcommands.add_parser("keygen")
    generate.add_argument("--key-file", required=True, type=Path)
    public = subcommands.add_parser("public-key")
    public.add_argument("--key-file", required=True, type=Path)
    for command in ("sign", "receipt"):
        operation = subcommands.add_parser(command)
        operation.add_argument("--key-file", required=True, type=Path)
        operation.add_argument("--enrollment-file", required=True, type=Path)
        operation.add_argument("--model", required=True)
        operation.add_argument("--conversation", required=True)
        if command == "sign":
            operation.add_argument("--handle")
        else:
            operation.add_argument("--request-nonce", required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "keygen":
            key = ContinuationKey.generate()
            write_key(arguments.key_file, key)
            print(json.dumps({"public_key": key.public_key}))
            return
        key = read_key(arguments.key_file)
        if arguments.command == "public-key":
            print(json.dumps({"public_key": key.public_key}))
            return
        enrollment = read_enrollment(arguments.enrollment_file)
        now = time.time()
        if (
            enrollment.client.expires_at <= now
            or arguments.model not in enrollment.client.models
        ):
            raise ValueError("enrollment expired or model not approved")
        credential = key.bind(enrollment.client)
        target = enrollment.target.model_copy(update={"model": str(arguments.model)})
        expiry = min(now + 30, enrollment.client.expires_at)
        if arguments.command == "receipt":
            query = ReceiptQuery(
                conversation_id=str(arguments.conversation),
                target=target,
                request_nonce=str(arguments.request_nonce),
                nonce=secrets.token_hex(32),
                expires_at=expiry,
            )
            print(credential.receipt(query).model_dump_json())
        else:
            raw = sys.stdin.buffer.read(MAX_BODY_BYTES + 1)
            request = ContinuationRequest(
                mode="continue" if arguments.handle else "start",
                conversation_id=str(arguments.conversation),
                handle=arguments.handle,
                target=target,
                body=InputBody.from_bytes(raw),
                nonce=secrets.token_hex(32),
                expires_at=expiry,
            )
            print(credential.sign(request).model_dump_json())
    except (OSError, ValueError):
        print("continuation client operation refused", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
