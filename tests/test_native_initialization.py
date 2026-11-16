"""Fresh native setup preserves disconnected authority and existing journals."""

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest

from skulk_openai.auth_contracts import AuthFailure
from skulk_openai.credentials import CredentialStore
from skulk_openai.state_transfer import (
    AdoptionBinding,
    NativeAdoption,
    enroll_native,
    initialize_native_state,
    native_proof_target,
    read_native_initialization,
    snapshot_state,
)

BINDINGS = (
    AdoptionBinding(
        legacy_id="synthetic.read",
        version="1.0.0",
        legacy_revision="a" * 16,
        native_id="synthetic.native.read",
        native_revision="b" * 64,
    ),
)


def target(root: Path, **changes: str) -> str:
    """Resolve a fresh synthetic installation using its exact independent bindings."""
    return native_proof_target(
        root,
        node_id=changes.get("node_id", "native-new-child"),
        bundle_id=changes.get("bundle_id", "synthetic.native"),
        bindings=BINDINGS,
        legacy_target_node=changes.get("legacy_target_node"),
    )


def journal_digest(root: Path) -> str:
    """Measure a closed protected journal without opening it for mutation."""
    return hashlib.sha256((root / "credentials.sqlite3").read_bytes()).hexdigest()


def test_fresh_setup_creates_only_disconnected_state_and_survives_restart(
    tmp_path: Path,
) -> None:
    """No migration, provider account, grant or checkpoint is needed for setup."""
    root = tmp_path / "new-installation"
    assert target(root) == "native-new-child"
    receipt = read_native_initialization(root)
    assert receipt.native_node_id == "native-new-child"
    assert receipt.bindings == BINDINGS
    assert not (root / "native-adoption.json").exists()
    assert not (root / "state-transfer.pending").exists()
    assert not (root / "continuation.key").exists()
    with sqlite3.connect(
        (root / "credentials.sqlite3").as_uri() + "?mode=ro", uri=True
    ) as connection:
        assert connection.execute("SELECT COUNT(*) FROM accounts").fetchone() == (0,)
        assert connection.execute(
            "SELECT value FROM metadata WHERE key='auth_epoch'"
        ).fetchone() == ("0",)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert tables == {"accounts", "metadata"}
    before = journal_digest(root)
    record = (root / "native-initialization.json").read_bytes()
    assert target(root) == "native-new-child"
    assert journal_digest(root) == before
    assert (root / "native-initialization.json").read_bytes() == record
    assert not root.stat().st_mode & 0o077
    assert not (root / "credentials.sqlite3").stat().st_mode & 0o077


@pytest.mark.parametrize("existing_fence", [False, True])
def test_empty_private_directory_and_stable_fence_can_be_initialized(
    tmp_path: Path, existing_fence: bool
) -> None:
    """Selecting an empty folder works without replacing an existing lock inode."""
    root = tmp_path / "empty"
    root.mkdir(mode=0o700)
    fence = root / "application-worker.lock"
    inode: int | None = None
    if existing_fence:
        fence.touch(mode=0o600)
        inode = fence.stat().st_ino
    assert target(root) == "native-new-child"
    assert inode in (None, fence.stat().st_ino)


@pytest.mark.parametrize("existing", ["file", "journal", "pending"])
def test_fresh_setup_never_repurposes_existing_state(
    tmp_path: Path, existing: str
) -> None:
    """Even an empty existing journal must be explicitly adopted, never reset."""
    root = tmp_path / "existing"
    root.mkdir(mode=0o700)
    if existing == "journal":
        CredentialStore(root)
    else:
        (
            root / ("state-transfer.pending" if existing == "pending" else "keep.txt")
        ).write_bytes(b"existing private data")
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    with pytest.raises(AuthFailure):
        target(root)
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before


@pytest.mark.parametrize("field", ["node_id", "bundle_id", "legacy_target_node"])
def test_fresh_identity_cannot_be_renamed_from_configuration(
    tmp_path: Path, field: str
) -> None:
    """A new caller or bundle cannot silently take over a previously bound root."""
    root = tmp_path / "bound"
    target(root)
    before = journal_digest(root)
    with pytest.raises(AuthFailure):
        target(root, **{field: "different"})
    assert journal_digest(root) == before


def test_fresh_setup_refuses_a_symlink_or_unprotected_root(tmp_path: Path) -> None:
    """No contents are created through a foreign path or by weakening permissions."""
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(AuthFailure):
        target(link)
    assert list(real.iterdir()) == []
    real.chmod(0o777)
    try:
        with pytest.raises(AuthFailure):
            target(real)
        assert list(real.iterdir()) == []
    finally:
        real.chmod(0o700)


def test_fresh_receipt_requires_protected_permissions(tmp_path: Path) -> None:
    """An exposed receipt is refused without creating or resetting anything."""
    root = tmp_path / "bound"
    target(root)
    receipt = root / "native-initialization.json"
    before = journal_digest(root)
    receipt.chmod(0o644)
    with pytest.raises(AuthFailure):
        target(root)
    assert journal_digest(root) == before


def test_fresh_receipt_refuses_a_different_journal_without_reset(
    tmp_path: Path,
) -> None:
    """Copying another store's database cannot attach its account history here."""
    root = tmp_path / "bound"
    target(root)
    foreign = CredentialStore(tmp_path / "foreign")
    copied = foreign.path.read_bytes()
    (root / "credentials.sqlite3").write_bytes(copied)
    with pytest.raises(AuthFailure):
        target(root)
    assert (root / "credentials.sqlite3").read_bytes() == copied


def test_fresh_receipt_refuses_different_contract_bindings(tmp_path: Path) -> None:
    """An existing journal is not silently rebound when callable identities change."""
    root = tmp_path / "bound"
    target(root)
    before = journal_digest(root)
    changed = BINDINGS[0].model_copy(update={"native_revision": "c" * 64})
    with pytest.raises(AuthFailure):
        native_proof_target(
            root,
            node_id="native-new-child",
            bundle_id="synthetic.native",
            bindings=(changed,),
            legacy_target_node=None,
        )
    assert journal_digest(root) == before


def test_interrupted_creation_remains_quarantined_for_all_applications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure between journal and receipt publication cannot expose half a store."""
    root = tmp_path / "interrupted"
    original = os.link

    def interrupted(source: Path, destination: Path, *, follow_symlinks: bool) -> None:
        if destination.name == "native-initialization.json":
            raise OSError("synthetic interrupted receipt publication")
        original(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", interrupted)
    with pytest.raises(OSError, match="interrupted receipt"):
        target(root)
    assert (root / "state-transfer.pending").is_file()
    before = journal_digest(root)
    for open_store in (lambda: target(root), lambda: CredentialStore(root)):
        with pytest.raises(AuthFailure):
            open_store()
    assert journal_digest(root) == before


def test_explicit_adoption_keeps_its_original_proof_identity(tmp_path: Path) -> None:
    """A genuine snapshot receipt remains supported without converting its identity."""
    store = CredentialStore(tmp_path / "old")
    snapshot = snapshot_state(store, tmp_path / "snapshot")
    enroll_native(
        store,
        tmp_path / "snapshot",
        NativeAdoption(
            store_host_id=store.host_id(),
            legacy_target_node="original-application-node",
            native_node_id="native-new-child",
            native_bundle_id="synthetic.native",
            bindings=BINDINGS,
            snapshot_state_digest=snapshot.state_digest,
        ),
    )
    assert target(store.root) == "original-application-node"
    assert not (store.root / "native-initialization.json").exists()
    before = journal_digest(store.root)
    with pytest.raises(AuthFailure):
        initialize_native_state(
            store.root,
            node_id="native-new-child",
            bundle_id="synthetic.native",
            bindings=BINDINGS,
        )
    assert journal_digest(store.root) == before
