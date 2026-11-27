"""Native identity and backup reads recover real interrupted SQLite writes."""

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest

from skulk_openai.auth_contracts import AuthFailure
from skulk_openai.credentials import CredentialStore
from skulk_openai.state_transfer import (
    NativeAdoption,
    application_fence,
    enroll_native,
    initialize_native_state,
    open_existing_state,
    read_native_adoption,
    read_native_initialization,
    snapshot_state,
    verify_snapshot,
)
from tests.test_checkpoints import first, query, ready
from tests.test_native_initialization import BINDINGS

_CRASH = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA cache_size=2')
connection.execute('BEGIN IMMEDIATE')
connection.execute("UPDATE metadata SET value='uncommitted-host' WHERE key='host_id'")
connection.execute(
    "UPDATE metadata SET value='dirty-' || value WHERE key LIKE 'fixture-page-%'"
)
connection.execute("INSERT INTO metadata VALUES ('uncommitted-marker', 'discard-me')")
os._exit(0)
"""


def seed_pages(store: CredentialStore) -> None:
    """Retain ordinary metadata pages which force a real transaction cache spill."""
    with store.transaction() as connection:
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [(f"fixture-page-{index}", "committed" * 512) for index in range(40)],
        )


def crash_write(root: Path) -> None:
    """Exit a separate writer without closing its transaction or faking a journal."""
    result = subprocess.run(
        [sys.executable, "-c", _CRASH, str(root / "credentials.sqlite3")],
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert not result.stdout and not result.stderr
    journal = root / "credentials.sqlite3-journal"
    assert journal.stat().st_size > 512 and journal.read_bytes()[:8] != bytes(8)
    with sqlite3.connect(
        (root / "credentials.sqlite3").as_uri() + "?mode=ro", uri=True
    ) as connection:
        with pytest.raises(sqlite3.OperationalError) as failure:
            connection.execute("SELECT value FROM metadata WHERE key='host_id'")
    assert failure.value.sqlite_errorcode == sqlite3.SQLITE_READONLY_ROLLBACK


@pytest.mark.parametrize("kind", ["initialized", "adopted", "snapshot"])
def test_existing_identity_and_snapshot_recover_an_interrupted_write(
    tmp_path: Path, kind: Literal["initialized", "adopted", "snapshot"]
) -> None:
    """Only SQLite rolls back the unfinished write; committed identity stays intact."""
    root = tmp_path / "state"
    if kind == "initialized":
        initialize_native_state(
            root, node_id="child", bundle_id="synthetic.native", bindings=BINDINGS
        )
    store = CredentialStore(root)
    seed_pages(store)
    host = store.host_id()
    epoch = store.epoch()
    before = snapshot_state(store, tmp_path / "before")
    if kind == "adopted":
        enroll_native(
            store,
            tmp_path / "before",
            NativeAdoption(
                store_host_id=host,
                legacy_target_node="original-proof-target",
                native_node_id="child",
                native_bundle_id="synthetic.native",
                bindings=BINDINGS,
                snapshot_state_digest=before.state_digest,
            ),
        )
    records = {p.name: p.read_bytes() for p in root.glob("native-*.json")}
    crash_write(root)
    if kind == "initialized":
        assert read_native_initialization(root).store_host_id == host
    elif kind == "adopted":
        assert read_native_adoption(root).store_host_id == host
    reopened = open_existing_state(root)
    assert reopened.host_id() == host and reopened.epoch() == epoch
    after = snapshot_state(reopened, tmp_path / "after")
    assert after.state_digest == before.state_digest
    assert verify_snapshot(tmp_path / "after") == after
    assert {p.name: p.read_bytes() for p in root.glob("native-*.json")} == records


async def test_recovery_keeps_committed_checkpoint_and_replay_history(
    tmp_path: Path,
) -> None:
    """A genuine encrypted checkpoint and its consumed nonces survive the crash."""
    store, credential, _ = await ready(tmp_path / "account")
    receipt = first(store, credential)
    seed_pages(store.credentials)
    before = snapshot_state(store.credentials, tmp_path / "before")
    crash_write(store.credentials.root)
    reopened = open_existing_state(store.credentials.root)
    after = snapshot_state(reopened, tmp_path / "after")
    assert before.state_digest == after.state_digest
    assert query(store, credential, receipt) == receipt


@pytest.mark.parametrize("guard", ["active", "pending", "linked-journal"])
def test_recovery_refuses_active_quarantined_or_linked_state(
    tmp_path: Path, guard: Literal["active", "pending", "linked-journal"]
) -> None:
    """Recovery never bypasses stable serving fences or unsafe-file guards."""
    root = tmp_path / "state"
    initialize_native_state(
        root, node_id="child", bundle_id="synthetic.native", bindings=BINDINGS
    )
    seed_pages(CredentialStore(root))
    crash_write(root)
    journal = root / "credentials.sqlite3-journal"
    if guard == "linked-journal":
        retained = tmp_path / "retained-journal"
        journal.rename(retained)
        journal.symlink_to(retained)
    elif guard == "pending":
        (root / "state-transfer.pending").write_text("interrupted publication")
    database_before = hashlib.sha256(
        (root / "credentials.sqlite3").read_bytes()
    ).hexdigest()
    journal_before = journal.read_bytes()

    def refused() -> None:
        """Check both existing-state entry points without changing either journal."""
        for read in (read_native_initialization, open_existing_state):
            with pytest.raises(AuthFailure):
                read(root)

    if guard == "active":
        with application_fence(root):
            refused()
    else:
        refused()
    assert (
        hashlib.sha256((root / "credentials.sqlite3").read_bytes()).hexdigest()
        == database_before
    )
    assert journal.read_bytes() == journal_before


def test_recovery_does_not_authorize_a_different_committed_store(
    tmp_path: Path,
) -> None:
    """Rolling back an interrupted write never replaces a foreign committed ID."""
    root = tmp_path / "state"
    initialize_native_state(
        root, node_id="child", bundle_id="synthetic.native", bindings=BINDINGS
    )
    store = CredentialStore(root)
    seed_pages(store)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE metadata SET value='different-committed-store' WHERE key='host_id'"
        )
    receipt = (root / "native-initialization.json").read_bytes()
    crash_write(root)
    with pytest.raises(AuthFailure):
        read_native_initialization(root)
    with sqlite3.connect(
        (root / "credentials.sqlite3").as_uri() + "?mode=ro", uri=True
    ) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='host_id'"
        ).fetchone() == ("different-committed-store",)
    assert (root / "native-initialization.json").read_bytes() == receipt
