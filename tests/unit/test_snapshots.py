"""Tests for snapshot primitives."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lattice.core import file_io as file_io_module
from lattice.core import snapshots as snapshots_module
from lattice.core.snapshots import (
    MAX_SNAPSHOT_METADATA_BYTES,
    create_global_snapshot,
    create_snapshot,
    find_snapshot_dir,
    list_snapshots_from_db,
    new_snapshot_id,
    read_snapshot_after_content,
    read_snapshot_before_content,
    read_snapshot_metadata,
    record_snapshot_in_db,
)


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

    snapshots_dir = tmp_path / ".lattice" / "snapshots"
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
