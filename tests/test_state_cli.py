"""Offline operator commands never initialize an absent or foreign source store."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from skulk_openai.credentials import CredentialStore
from skulk_openai.settings import SettingsStore


def command(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    """Execute the real installed CLI without provider accounts or native imports."""
    return subprocess.run(
        [sys.executable, "-m", "skulk_openai.state_cli", *map(str, arguments)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_explicit_empty_store_snapshot_and_quarantined_restore(tmp_path: Path) -> None:
    root, backup, restored = (
        tmp_path / name for name in ("state", "backup", "restore")
    )
    initialized = command("initialize", "--state-root", root)
    assert initialized.returncode == 0
    original = CredentialStore(root)
    identity, epoch = original.host_id(), original.epoch()
    assert command("snapshot", "--state-root", root, "--output", backup).returncode == 0
    assert command("verify", "--backup", backup).returncode == 0
    result = command("restore", "--backup", backup, "--output", restored)
    assert result.returncode == 0 and not result.stderr
    assert json.loads(result.stdout) == {"operation": "restore", "state": "quarantined"}
    current = CredentialStore(restored)
    assert current.host_id() == identity and current.epoch() > epoch
    assert not SettingsStore(current).read().inference_enabled
    assert not (restored / "native-adoption.json").exists()
    # Existing roots are never overwritten, including a quarantined restore.
    assert command("restore", "--backup", backup, "--output", restored).returncode == 1
    assert CredentialStore(root).epoch() == epoch


@pytest.mark.parametrize("foreign", [False, True])
def test_snapshot_refuses_without_initializing_source(
    tmp_path: Path, foreign: bool
) -> None:
    root, backup = tmp_path / "state", tmp_path / "backup"
    root.mkdir(mode=0o700)
    database = root / "credentials.sqlite3"
    if foreign:
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE unrelated(secret TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('synthetic-private')")
        database.chmod(0o600)
    before = database.read_bytes() if foreign else None
    result = command("snapshot", "--state-root", root, "--output", backup)
    assert result.returncode == 1
    assert not result.stdout and "synthetic-private" not in result.stderr
    assert (database.read_bytes() if database.exists() else None) == before
    assert not backup.exists()
