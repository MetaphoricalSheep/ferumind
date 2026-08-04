"""Tests for the watcher worker's debounce and event classification."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lattice.core.errors import ProjectNotFoundError
from lattice.core.operations import list_operations
from lattice.core.paths import WorkspaceRoot
from lattice.core.snapshots import list_snapshots_from_db
from lattice.db.database import Database
from lattice.workers import watcher as watcher_module
from lattice.workers.watcher import (
    FileDebouncer,
    classify_event_path,
    handle_detected_change,
)


class TestFileDebouncer:
    def test_events_coalesce_until_quiet(self) -> None:
        bouncer = FileDebouncer(quiet_seconds=5.0)
        bouncer.note_event("a", now=0.0)
        bouncer.note_event("a", now=3.0)  # still active: window restarts
        assert bouncer.take_due(now=6.0) == []
        assert bouncer.take_due(now=8.5) == ["a"]
        assert bouncer.pending_count() == 0

    def test_independent_files_flush_independently(self) -> None:
        bouncer = FileDebouncer(quiet_seconds=5.0)
        bouncer.note_event("a", now=0.0)
        bouncer.note_event("b", now=4.0)
        assert bouncer.take_due(now=5.5) == ["a"]
        assert bouncer.take_due(now=9.5) == ["b"]

    def test_snapshot_rate_limit_per_file(self) -> None:
        bouncer = FileDebouncer(min_snapshot_interval=60.0)
        assert bouncer.allow_snapshot("a", now=0.0)
        assert not bouncer.allow_snapshot("a", now=30.0)
        assert bouncer.allow_snapshot("b", now=30.0)
        assert bouncer.allow_snapshot("a", now=61.0)

    def test_snapshot_tracking_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(watcher_module, "MAX_SNAPSHOT_TRACKING_KEYS", 2)
        bouncer = FileDebouncer()

        assert bouncer.allow_snapshot("a", now=1.0)
        assert bouncer.allow_snapshot("b", now=2.0)
        assert bouncer.allow_snapshot("c", now=3.0)
        assert bouncer.snapshot_tracking_count() == 2

    def test_pending_event_tracking_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(watcher_module, "MAX_PENDING_EVENT_KEYS", 2)
        bouncer = FileDebouncer()

        bouncer.note_event("a", now=1.0)
        bouncer.note_event("b", now=2.0)
        bouncer.note_event("c", now=3.0)

        assert bouncer.pending_count() == 2
        assert bouncer.take_due(now=10.0) == ["b", "c"]

    def test_failed_snapshot_reservation_can_be_retried(self) -> None:
        bouncer = FileDebouncer(min_snapshot_interval=60.0)
        assert bouncer.allow_snapshot("a", now=1.0)
        bouncer.release_snapshot_reservation("a", reserved_at=1.0)
        assert bouncer.allow_snapshot("a", now=2.0)


class TestClassifyEventPath:
    def test_maps_project_markdown(self, workspace: WorkspaceRoot) -> None:
        changed = Path(workspace) / "projects" / "demo" / "canvases" / "log.md"
        assert classify_event_path(workspace, changed) == ("demo", "canvases/log.md")

    def test_ignores_non_markdown_hidden_and_internals(self, workspace: WorkspaceRoot) -> None:
        base = Path(workspace) / "projects" / "demo"
        assert classify_event_path(workspace, base / "canvases" / "img.png") is None
        assert classify_event_path(workspace, base / ".lattice" / "snapshots" / "x.md") is None
        assert classify_event_path(workspace, Path(workspace) / "system" / "meta.yml") is None
        assert classify_event_path(workspace, Path("/etc/passwd")) is None
        assert classify_event_path(workspace, base / "spine.md") == ("demo", "spine.md")


def test_handle_detected_change_snapshots_and_logs(
    database: Database, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    from lattice.core.indexer import index_project

    index_project(conn, workspace, project)
    spine = Path(workspace) / "projects" / project / "spine.md"
    spine.write_text(spine.read_text(encoding="utf-8") + "\nhand edit\n", encoding="utf-8")

    handle_detected_change(database, workspace, project, "spine.md", snapshot=True)

    snapshots = list_snapshots_from_db(conn, project_key=project, target_path="spine.md")
    assert any(s.reason == "watch_detect" for s in snapshots)
    assert any(op.source == "watcher" for op in list_operations(conn, project, limit=10))


def test_handle_detected_change_without_snapshot(
    database: Database, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    from lattice.core.indexer import index_project

    index_project(conn, workspace, project)
    spine = Path(workspace) / "projects" / project / "spine.md"
    spine.write_text(spine.read_text(encoding="utf-8") + "\nanother edit\n", encoding="utf-8")

    handle_detected_change(database, workspace, project, "spine.md", snapshot=False)

    snapshots = list_snapshots_from_db(conn, project_key=project, target_path="spine.md")
    assert not any(s.reason == "watch_detect" for s in snapshots)
    assert any(op.source == "watcher" for op in list_operations(conn, project, limit=10))


def test_snapshot_request_does_not_snapshot_a_content_identical_touch(
    database: Database,
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    from lattice.core.indexer import index_project

    index_project(conn, workspace, project)
    spine = Path(workspace) / "projects" / project / "spine.md"
    content = spine.read_text(encoding="utf-8")
    spine.write_text(content, encoding="utf-8")

    handle_detected_change(database, workspace, project, "spine.md", snapshot=True)

    snapshots = list_snapshots_from_db(conn, project_key=project, target_path="spine.md")
    assert not any(s.reason == "watch_detect" for s in snapshots)


def test_handle_detected_change_rejects_unregistered_project(
    database: Database, workspace: WorkspaceRoot
) -> None:
    unknown = workspace / "projects" / "unknown"
    unknown.mkdir()
    (unknown / "note.md").write_text("unregistered\n", encoding="utf-8")

    with pytest.raises(ProjectNotFoundError):
        handle_detected_change(database, workspace, "unknown", "note.md", snapshot=True)
