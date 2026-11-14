"""Explicit offline state operations; diagnostics contain no account or window data."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Literal, final

from .auth_contracts import AuthFailure
from .credentials import CredentialStore
from .state_transfer import (
    NativeAdoption,
    enroll_native,
    open_existing_state,
    restore_quarantined,
    snapshot_state,
    verify_snapshot,
)


@final
class Arguments(argparse.Namespace):
    """Validated command selection and explicit private paths."""

    operation: Literal["initialize", "snapshot", "verify", "adopt", "restore"]
    state_root: Path | None
    backup: Path | None
    output: Path | None
    native_node_id: str | None
    legacy_target_node: str | None
    mock: bool


def _path(value: Path | None) -> Path:
    if value is None or not value.is_absolute():
        raise ValueError("an absolute private path is required")
    return value


def main() -> None:
    """Run one operator-selected offline operation without implicit default stores."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.set_defaults(
        state_root=None,
        backup=None,
        output=None,
        native_node_id=None,
        legacy_target_node=None,
        mock=False,
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    initialize = commands.add_parser("initialize")
    initialize.add_argument("--state-root", required=True, type=Path)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--state-root", required=True, type=Path)
    snapshot.add_argument("--output", required=True, type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("--backup", required=True, type=Path)
    adopt = commands.add_parser("adopt")
    adopt.add_argument("--state-root", required=True, type=Path)
    adopt.add_argument("--backup", required=True, type=Path)
    adopt.add_argument("--native-node-id", required=True)
    adopt.add_argument("--legacy-target-node", required=True)
    adopt.add_argument("--mock", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(namespace=Arguments())
    try:
        if arguments.operation == "initialize":
            root = _path(arguments.state_root)
            root.mkdir(mode=0o700)
            CredentialStore(root)
        elif arguments.operation == "snapshot":
            snapshot_state(
                open_existing_state(_path(arguments.state_root)),
                _path(arguments.output),
            )
        elif arguments.operation == "verify":
            verify_snapshot(_path(arguments.backup))
        elif arguments.operation == "restore":
            restore_quarantined(_path(arguments.backup), _path(arguments.output))
        else:
            # Native author dependencies are optional for ordinary offline
            # snapshots; adoption explicitly selects the released author cohort.
            from .native_contracts import adoption_bindings

            if arguments.native_node_id is None or arguments.legacy_target_node is None:
                raise ValueError("explicit application identities are required")
            store = open_existing_state(_path(arguments.state_root))
            backup = _path(arguments.backup)
            receipt = verify_snapshot(backup)
            enroll_native(
                store,
                backup,
                NativeAdoption(
                    store_host_id=store.host_id(),
                    legacy_target_node=arguments.legacy_target_node,
                    native_node_id=arguments.native_node_id,
                    native_bundle_id="reference.openai.native.mock"
                    if arguments.mock
                    else "reference.openai.native",
                    bindings=adoption_bindings(mock=arguments.mock),
                    snapshot_state_digest=receipt.state_digest,
                ),
            )
    except ImportError:
        print(
            "Native adoption requires the public managed and native-runtime groups.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except (AuthFailure, OSError, ValueError, sqlite3.Error):
        print(
            "Protected state operation refused; existing state was not replaced.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(
        json.dumps(
            {
                "operation": arguments.operation,
                "state": "quarantined"
                if arguments.operation == "restore"
                else "complete",
            }
        )
    )


if __name__ == "__main__":
    main()
