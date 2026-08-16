"""Tests for snapshot primitives."""

from __future__ import annotations

import hashlib
import itertools
import sqlite3
from pathlib import Path

import pytest

from ferumind.core import file_io as file_io_module
from ferumind.core import snapshots as snapshots_module
from ferumind.core.blob_store import blob_path, blob_store_root, sweep_unreferenced
from ferumind.core.snapshots import (
    MAX_SNAPSHOT_METADATA_BYTES,
    create_binary_replacement_snapshot,
    create_global_snapshot,
    create_snapshot,
    create_upload_snapshot,
    find_snapshot_dir,
    list_snapshots_from_db,
    new_snapshot_id,
    read_snapshot_after_content,
    read_snapshot_before_content,
    read_snapshot_metadata,
    record_snapshot_in_db,
)


def _stored_digests(store_root: Path) -> set[str]:
    """Every blob currently in *store_root*, by digest."""
    if not store_root.is_dir():
        return set()
    return {blob.name for shard in store_root.iterdir() for blob in shard.iterdir()}


def test_create_snapshot_writes_before_after_diff_metadata(tmp_path: Path) -> None:
    snapshot_id = new_snapshot_id()
    snapshot_dir = create_snapshot(
        tmp_path,
        project_key="demo",
        target_path="canvases/a.md",
        before_content="old\n",
        after_content="new\n",
        reason="apply_patch",
        snapshot_id=snapshot_id,
    )
    assert read_snapshot_before_content(snapshot_dir, "canvases/a.md") == "old\n"
    assert read_snapshot_after_content(snapshot_dir, "canvases/a.md") == "new\n"
    diff = (snapshot_dir / "diff.patch").read_text(encoding="utf-8")
    assert "-old" in diff
    assert "+new" in diff
    metadata = read_snapshot_metadata(snapshot_dir)
    assert metadata is not None
    assert metadata.id == snapshot_id
    assert metadata.target_path == "canvases/a.md"
    assert metadata.before_sha256 is not None
    assert metadata.before_size_bytes == len(b"old\n")
    assert metadata.after_sha256 is not None
    assert metadata.after_size_bytes == len(b"new\n")
    assert find_snapshot_dir(tmp_path, snapshot_id) == snapshot_dir
    assert snapshot_dir.stat().st_mode & 0o777 == 0o700
    assert (snapshot_dir / "metadata.json").stat().st_mode & 0o777 == 0o600
    assert (snapshot_dir / "before/canvases/a.md").stat().st_mode & 0o777 == 0o600


def test_snapshot_root_is_re_privatized_even_after_being_widened(tmp_path: Path) -> None:
    """The deliberate half of S-09, pinned so a later cleanup cannot undo it.

    Operator-owned directories keep whatever mode the operator sets, but
    ``.ferumind/`` is Ferumind's own state and snapshots hold verbatim copies
    of document bodies. ``SECURITY.md`` promises it stays private, so this one
    is re-asserted on every touch rather than only set on create.
    """
    snapshots_root = tmp_path / ".ferumind" / "snapshots"
    create_snapshot(
        tmp_path,
        project_key="demo",
        target_path="canvases/a.md",
        before_content="old\n",
        after_content="new\n",
        reason="apply_patch",
        snapshot_id=new_snapshot_id(),
    )
    snapshots_root.chmod(0o755)

    create_snapshot(
        tmp_path,
        project_key="demo",
        target_path="canvases/b.md",
        before_content="old\n",
        after_content="new\n",
        reason="apply_patch",
        snapshot_id=new_snapshot_id(),
    )

    assert snapshots_root.stat().st_mode & 0o777 == 0o700


def test_missing_sides_are_tolerated(tmp_path: Path) -> None:
    snapshot_id = new_snapshot_id()
    snapshot_dir = create_snapshot(
        tmp_path,
        project_key="demo",
        target_path="canvases/a.md",
        before_content=None,
        after_content="created\n",
        reason="create_document",
        snapshot_id=snapshot_id,
    )
    assert read_snapshot_before_content(snapshot_dir, "canvases/a.md") is None
    assert read_snapshot_after_content(snapshot_dir, "canvases/a.md") == "created\n"


def test_failed_snapshot_construction_removes_partial_user_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_atomic_write_text = file_io_module.atomic_write_text

    def fail_on_diff(path: Path, content: str) -> None:
        if path.name == "diff.patch":
            raise OSError("injected snapshot failure")
        real_atomic_write_text(path, content)

    monkeypatch.setattr(snapshots_module, "atomic_write_text", fail_on_diff)
    with pytest.raises(OSError, match="injected snapshot failure"):
        create_snapshot(
            tmp_path,
            project_key="demo",
            target_path="canvases/a.md",
            before_content="private content\n",
            after_content="new content\n",
            reason="apply_patch",
            snapshot_id=new_snapshot_id(),
        )

    snapshots_dir = tmp_path / ".ferumind" / "snapshots"
    assert snapshots_dir.is_dir()
    assert list(snapshots_dir.iterdir()) == []


def test_snapshot_before_content_integrity_checks_fail_closed(tmp_path: Path) -> None:
    snapshot_dir = create_snapshot(
        tmp_path,
        project_key="demo",
        target_path="canvases/a.md",
        before_content="old\n",
        after_content="new\n",
        reason="apply_patch",
        snapshot_id=new_snapshot_id(),
    )
    metadata = read_snapshot_metadata(snapshot_dir)
    assert metadata is not None
    target = snapshot_dir / "before/canvases/a.md"
    target.write_text("substituted\n", encoding="utf-8")

    assert (
        read_snapshot_before_content(
            snapshot_dir,
            "canvases/a.md",
            expected_sha256=metadata.before_sha256,
            expected_size_bytes=metadata.before_size_bytes,
        )
        is None
    )


def test_global_snapshot_captures_multiple_files(tmp_path: Path) -> None:
    snapshot_id = new_snapshot_id()
    snapshot_dir = create_global_snapshot(
        tmp_path,
        snapshot_id=snapshot_id,
        operation_type="create_project",
        target_project_key="demo",
        reason="create_project",
        before_files={},
        after_files={"projects/demo/spine.md": "# Demo\n", "system/projects.yml": "projects:\n"},
    )
    assert (snapshot_dir / "after" / "projects/demo/spine.md").is_file()
    assert (snapshot_dir / "metadata.json").is_file()


def test_snapshot_registry_round_trip(conn: sqlite3.Connection) -> None:
    record_snapshot_in_db(
        conn,
        snapshot_id="snap-1",
        project_key="demo",
        target_path="canvases/a.md",
        snapshot_dir="/tmp/x",
        reason="apply_patch",
    )
    rows = list_snapshots_from_db(conn, project_key="demo")
    assert [r.id for r in rows] == ["snap-1"]
    assert list_snapshots_from_db(conn, project_key="demo", target_path="other.md") == []
    assert (
        list_snapshots_from_db(conn, project_key="demo", target_path="canvases/a.md")[0].reason
        == "apply_patch"
    )


def test_find_snapshot_dir_missing(tmp_path: Path) -> None:
    assert find_snapshot_dir(tmp_path, "nope") is None


def test_oversized_snapshot_metadata_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_bytes(b"x" * (MAX_SNAPSHOT_METADATA_BYTES + 1))
    assert read_snapshot_metadata(tmp_path) is None


class TestPayloadsAreStoredOnce:
    """STORE-01: a payload costs its size once, however many names hold it."""

    def test_a_snapshot_payload_shares_its_blobs_inode(self, tmp_path: Path) -> None:
        snapshot_dir = create_snapshot(
            tmp_path,
            project_key="demo",
            target_path="canvases/a.md",
            before_content="old\n",
            after_content="new\n",
            reason="apply_patch",
            snapshot_id=new_snapshot_id(),
        )

        store = blob_store_root(tmp_path)
        for side, content in (("before", b"old\n"), ("after", b"new\n")):
            payload = snapshot_dir / side / "canvases/a.md"
            digest = hashlib.sha256(content).hexdigest()
            assert payload.stat().st_ino == blob_path(store, digest).stat().st_ino

    def test_identical_before_and_after_content_costs_one_payload(self, tmp_path: Path) -> None:
        snapshot_dir = create_snapshot(
            tmp_path,
            project_key="demo",
            target_path="canvases/a.md",
            before_content="unchanged\n",
            after_content="unchanged\n",
            reason="apply_patch",
            snapshot_id=new_snapshot_id(),
        )

        before = snapshot_dir / "before/canvases/a.md"
        after = snapshot_dir / "after/canvases/a.md"
        assert before.stat().st_ino == after.stat().st_ino
        assert len(_stored_digests(blob_store_root(tmp_path))) == 1

    def test_an_edit_chain_of_n_steps_stores_n_plus_one_versions(self, tmp_path: Path) -> None:
        """The defect this ticket exists for: every version was stored twice."""
        versions = [f"version {index}\n" for index in range(5)]
        for before, after in itertools.pairwise(versions):
            create_snapshot(
                tmp_path,
                project_key="demo",
                target_path="canvases/a.md",
                before_content=before,
                after_content=after,
                reason="apply_patch",
                snapshot_id=new_snapshot_id(),
            )

        assert len(_stored_digests(blob_store_root(tmp_path))) == len(versions)

    def test_an_upload_snapshot_hands_back_the_blob_it_stored(self, tmp_path: Path) -> None:
        content = b"\x89PNG not really\n"
        result = create_upload_snapshot(
            tmp_path,
            project_key="demo",
            content_path="library/photo.png",
            content_bytes=content,
            metadata_path="library/photo.png.ferumind.json",
            metadata_text="{}\n",
            reason="upload_library_file",
            snapshot_id=new_snapshot_id(),
        )

        store = blob_store_root(tmp_path)
        payload = result.snapshot_dir / "after/library/photo.png"
        assert result.content_ref.digest == hashlib.sha256(content).hexdigest()
        assert result.content_ref.size_bytes == len(content)
        assert payload.stat().st_ino == blob_path(store, result.content_ref.digest).stat().st_ino
        assert payload.read_bytes() == content

    def test_a_binary_replacement_stores_both_sides_once(self, tmp_path: Path) -> None:
        before_bytes = b"original image bytes\n"
        after_bytes = b"compressed image bytes\n"
        result = create_binary_replacement_snapshot(
            tmp_path,
            project_key="demo",
            content_path="library/photo.jpg",
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            reason="compress_images",
            snapshot_id=new_snapshot_id(),
        )

        store = blob_store_root(tmp_path)
        assert result.before_ref.digest == hashlib.sha256(before_bytes).hexdigest()
        assert result.after_ref.digest == hashlib.sha256(after_bytes).hexdigest()
        assert _stored_digests(store) == {result.before_ref.digest, result.after_ref.digest}
        for side, ref, payload_bytes in (
            ("before", result.before_ref, before_bytes),
            ("after", result.after_ref, after_bytes),
        ):
            payload = result.snapshot_dir / side / "library/photo.jpg"
            assert payload.read_bytes() == payload_bytes
            assert payload.stat().st_ino == blob_path(store, ref.digest).stat().st_ino

    def test_a_global_snapshot_uses_the_workspace_store(self, tmp_path: Path) -> None:
        snapshot_dir = create_global_snapshot(
            tmp_path,
            snapshot_id=new_snapshot_id(),
            operation_type="create_project",
            target_project_key="demo",
            reason="create_project",
            before_files={},
            after_files={
                "projects/demo/spine.md": "# Demo\n",
                "projects/demo/copy.md": "# Demo\n",
            },
        )

        store = blob_store_root(tmp_path)
        first = snapshot_dir / "after/projects/demo/spine.md"
        second = snapshot_dir / "after/projects/demo/copy.md"
        assert _stored_digests(store) == {hashlib.sha256(b"# Demo\n").hexdigest()}
        assert first.stat().st_ino == second.stat().st_ino

    def test_a_failed_construction_publishes_no_snapshot_and_leaks_no_payload(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A crash mid-construction leaves nothing to restore from.

        The blob it had already stored survives the ``rmtree`` — nothing else
        references it, so it is the sweeper's to reclaim, not the snapshot's.
        """
        real_atomic_write_text = file_io_module.atomic_write_text

        def fail_on_metadata(path: Path, content: str) -> None:
            if path.name == "metadata.json":
                raise OSError("injected snapshot failure")
            real_atomic_write_text(path, content)

        monkeypatch.setattr(snapshots_module, "atomic_write_text", fail_on_metadata)
        with pytest.raises(OSError, match="injected snapshot failure"):
            create_snapshot(
                tmp_path,
                project_key="demo",
                target_path="canvases/a.md",
                before_content="private content\n",
                after_content="new content\n",
                reason="apply_patch",
                snapshot_id=new_snapshot_id(),
            )

        assert list((tmp_path / ".ferumind/snapshots").iterdir()) == []
        store = blob_store_root(tmp_path)
        assert len(_stored_digests(store)) == 2
        assert sweep_unreferenced(store).removed == 2
        assert _stored_digests(store) == set()
