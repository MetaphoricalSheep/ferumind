"""Retention: what ``ferumind prune`` reclaims, and what it must never touch.

Every test here runs against a real bootstrapped workspace under ``tmp_path``
with real snapshots, real blobs, and a real SQLite file. Ages are synthetic —
snapshot directories are renamed to the timestamp Ferumind would have written
and rows get explicit ``created_at`` values — because a retention window
measured in days cannot be reached by waiting.

The load-bearing test is :class:`TestUserContentIsUntouched`. Prune is pointed
at a workspace that is the only copy of somebody's knowledge, so "it deleted
the right things" is worth less than "it provably touched nothing else".
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import pytest
from pydantic import ValidationError as PydanticValidationError

from ferumind.core import retention
from ferumind.core.blob_store import blob_store_root, stored_blobs
from ferumind.core.errors import ProjectNotFoundError
from ferumind.core.observations import record_mcp_call_observation
from ferumind.core.operations import (
    OP_APPLIED,
    OP_DISCARDED,
    OP_FAILED,
    OP_PENDING,
    record_operation,
)
from ferumind.core.paths import WorkspaceRoot, contained_path, contained_project_root
from ferumind.core.retention import (
    STORE_BLOBS,
    STORE_GLOBAL_SNAPSHOTS,
    STORE_MIGRATION_BACKUPS,
    STORE_OBSERVATIONS,
    STORE_OPERATION_DIFFS,
    STORE_RUNTIME_LOG,
    STORE_SNAPSHOTS,
    STORE_SPENT_PROPOSALS,
    PruneReport,
    RetentionPolicy,
    RetentionPrerequisiteError,
    prune_workspace,
)
from ferumind.core.runtime_events import RUNTIME_LOG_RELATIVE_PATH
from ferumind.core.snapshots import (
    create_global_snapshot,
    create_snapshot,
    new_snapshot_id,
    record_snapshot_in_db,
)

STAMP_FORMAT = "%Y%m%dT%H%M%S"


class _FullDisk(NamedTuple):
    """A ``shutil.disk_usage`` result with nothing left."""

    total: int = 1
    used: int = 1
    free: int = 0


# ── Fixtures and builders ────────────────────────────────────────────────────


@pytest.fixture
def project_root(workspace: WorkspaceRoot, project: str) -> Path:
    return contained_project_root(workspace, project)


def _aged_stamp(age_days: int, *, offset_seconds: int = 0) -> str:
    moment = datetime.now(UTC) - timedelta(days=age_days, seconds=-offset_seconds)
    return moment.strftime(STAMP_FORMAT)


def _age_directory(directory: Path, age_days: int, *, offset_seconds: int = 0) -> Path:
    """Rename a real snapshot directory to the stamp it would carry when aged."""
    snapshot_id = directory.name.split("-", 1)[1]
    aged = directory.with_name(
        f"{_aged_stamp(age_days, offset_seconds=offset_seconds)}-{snapshot_id}"
    )
    directory.rename(aged)
    return aged


def aged_snapshot(
    conn: sqlite3.Connection,
    project_root: Path,
    project_key: str,
    *,
    age_days: int,
    content: str = "before\n",
) -> tuple[str, Path]:
    """Create a real snapshot and backdate it, registry row included."""
    snapshot_id = new_snapshot_id()
    directory = create_snapshot(
        project_root,
        project_key=project_key,
        target_path="canvases/plan.md",
        before_content=content,
        after_content=None,
        reason="test_retention",
        snapshot_id=snapshot_id,
    )
    aged = _age_directory(directory, age_days, offset_seconds=-len(content))
    record_snapshot_in_db(
        conn,
        snapshot_id=snapshot_id,
        project_key=project_key,
        target_path="canvases/plan.md",
        snapshot_dir=str(aged),
        reason="test_retention",
    )
    return snapshot_id, aged


def aged_global_snapshot(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    *,
    age_days: int,
) -> tuple[str, Path]:
    snapshot_id = new_snapshot_id()
    directory = create_global_snapshot(
        workspace,
        snapshot_id=snapshot_id,
        operation_type="test_retention",
        target_project_key=None,
        reason="test_retention",
        before_files={"system/meta.yml": "format: 1\n"},
        after_files={},
    )
    aged = _age_directory(directory, age_days)
    record_snapshot_in_db(
        conn,
        snapshot_id=snapshot_id,
        project_key="",
        target_path=None,
        snapshot_dir=str(aged),
        reason="test_retention",
    )
    return snapshot_id, aged


def aged_operation(
    conn: sqlite3.Connection,
    project_key: str,
    *,
    age_days: int,
    state: str = OP_APPLIED,
    operation_type: str = "apply_patch",
    diff_text: str | None = "--- a\n+++ b\n",
) -> str:
    operation_id = record_operation(
        conn,
        project_key=project_key,
        operation_type=operation_type,
        tool_name=operation_type,
        target_path="canvases/plan.md",
        base_sha256="a" * 64,
        after_sha256="b" * 64,
        diff_text=diff_text,
        snapshot_id="snap-1",
        state=state,
        expires_at=None if state != OP_PENDING else datetime.now(UTC).isoformat(),
    )
    conn.execute(
        "UPDATE operations SET created_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(days=age_days)).isoformat(), operation_id),
    )
    conn.commit()
    return operation_id


def aged_observation(conn: sqlite3.Connection, *, age_days: int) -> str:
    observation_id = record_mcp_call_observation(conn, tool_name="get_context", ok=True)
    conn.execute(
        "UPDATE mcp_call_observations SET created_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(days=age_days)).isoformat(), observation_id),
    )
    conn.commit()
    return observation_id


def backup_tarball(workspace: WorkspaceRoot, stamp: str) -> Path:
    base = contained_path(workspace, ".ferumind/backups")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = base / f"workspace-backup-{stamp}.tar.gz"
    path.write_bytes(b"x" * 4096)
    return path


def store(report: PruneReport, name: str, scope: str | None = None) -> retention.StoreReclaim:
    matches = [
        entry
        for entry in report.stores
        if entry.store == name and (scope is None or entry.scope == scope)
    ]
    assert matches, f"no {name} entry in {[entry.store for entry in report.stores]}"
    return matches[0]


def tree_manifest(root: Path) -> dict[str, str]:
    """Hash every file under *root* except Ferumind's own internal state."""
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part.startswith(".ferumind") for part in relative.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        manifest[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return manifest


# ── Dry run ──────────────────────────────────────────────────────────────────


class TestDryRunIsTheDefault:
    def test_prune_without_apply_deletes_nothing(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        snapshot_id, directory = aged_snapshot(conn, project_root, project, age_days=400)
        operation_id = aged_operation(conn, project, age_days=400)
        observation_id = aged_observation(conn, age_days=400)
        doomed_backup = backup_tarball(workspace, "20200101T000000000000")
        for index in range(2):
            backup_tarball(workspace, f"20260101T00000000000{index + 1}")
        snapshot_rows = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]

        report = prune_workspace(conn, workspace, policy=RetentionPolicy(keep_recent_snapshots=0))

        assert report.dry_run is True
        assert directory.is_dir()
        assert doomed_backup.is_file()
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == snapshot_rows
        assert (
            conn.execute("SELECT COUNT(*) FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()[
                0
            ]
            == 1
        )
        diff = conn.execute(
            "SELECT diff_text FROM operations WHERE id = ?", (operation_id,)
        ).fetchone()[0]
        assert diff is not None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM mcp_call_observations WHERE id = ?", (observation_id,)
            ).fetchone()[0]
            == 1
        )

    def test_dry_run_reports_what_a_real_run_would_take(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        aged_snapshot(conn, project_root, project, age_days=400)
        aged_operation(conn, project, age_days=400)
        aged_observation(conn, age_days=400)
        backup_tarball(workspace, "20200101T000000000000")
        backup_tarball(workspace, "20260101T000000000001")
        backup_tarball(workspace, "20260102T000000000002")
        policy = RetentionPolicy(keep_recent_snapshots=0)

        planned = prune_workspace(conn, workspace, policy=policy)
        applied = prune_workspace(conn, workspace, policy=policy, dry_run=False)

        for name in (STORE_SNAPSHOTS, STORE_MIGRATION_BACKUPS, STORE_OBSERVATIONS, STORE_BLOBS):
            assert store(planned, name).reclaimed == store(applied, name).reclaimed, name
        assert (
            store(planned, STORE_BLOBS).bytes_reclaimed
            == store(applied, STORE_BLOBS).bytes_reclaimed
        )

    @pytest.mark.usefixtures("project")
    def test_dry_run_records_no_operation(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot
    ) -> None:
        before = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
        report = prune_workspace(conn, workspace)
        assert report.operation_id is None
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == before


# ── The guarantee that matters ───────────────────────────────────────────────


class TestUserContentIsUntouched:
    def test_no_knowledge_file_changes_across_a_real_prune(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        for folder in ("archive", "memory", "canvases", "inbox", "library"):
            target = project_root / folder
            target.mkdir(parents=True, exist_ok=True)
            (target / "kept.md").write_text(f"# {folder}\n\nreal user knowledge\n")
        (project_root / "spine.md").write_text("# Spine\n\nthe spine\n")

        aged_snapshot(conn, project_root, project, age_days=400)
        aged_global_snapshot(conn, workspace, age_days=400)
        aged_operation(conn, project, age_days=400)
        aged_observation(conn, age_days=400)
        backup_tarball(workspace, "20200101T000000000000")
        before = tree_manifest(Path(workspace))

        prune_workspace(
            conn, workspace, policy=RetentionPolicy(keep_recent_snapshots=0), dry_run=False
        )

        assert tree_manifest(Path(workspace)) == before

    def test_a_directory_ferumind_did_not_name_is_left_alone(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project_root: Path
    ) -> None:
        base = contained_path(project_root, ".ferumind/snapshots")
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        stray = base / "not-a-snapshot"
        stray.mkdir()
        (stray / "payload").write_text("unknown provenance")
        undated = base / f"nostamp-{new_snapshot_id()}"
        undated.mkdir()

        prune_workspace(
            conn, workspace, policy=RetentionPolicy(keep_recent_snapshots=0), dry_run=False
        )

        assert stray.is_dir()
        assert undated.is_dir()


# ── Snapshots ────────────────────────────────────────────────────────────────


class TestSnapshotRetention:
    def test_expired_snapshot_and_row_go_together(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        snapshot_id, directory = aged_snapshot(conn, project_root, project, age_days=400)

        prune_workspace(
            conn, workspace, policy=RetentionPolicy(keep_recent_snapshots=0), dry_run=False
        )

        assert not directory.exists()
        assert (
            conn.execute("SELECT COUNT(*) FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()[
                0
            ]
            == 0
        )

    def test_recent_snapshots_survive(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        _, recent = aged_snapshot(conn, project_root, project, age_days=3, content="recent\n")
        _, old = aged_snapshot(conn, project_root, project, age_days=400, content="old\n")

        prune_workspace(
            conn, workspace, policy=RetentionPolicy(keep_recent_snapshots=0), dry_run=False
        )

        assert recent.is_dir()
        assert not old.exists()

    def test_the_newest_are_kept_even_when_every_one_is_expired(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        made = [
            aged_snapshot(conn, project_root, project, age_days=400 + index, content=f"v{index}\n")
            for index in range(5)
        ]

        prune_workspace(
            conn, workspace, policy=RetentionPolicy(keep_recent_snapshots=2), dry_run=False
        )

        surviving = [directory for _, directory in made if directory.is_dir()]
        assert len(surviving) == 2
        # The two kept are the newest, which are the two smallest ages.
        assert {directory.name for directory in surviving} == {
            made[0][1].name,
            made[1][1].name,
        }

    @pytest.mark.usefixtures("project")
    def test_global_snapshots_follow_the_same_rule(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot
    ) -> None:
        snapshot_id, directory = aged_global_snapshot(conn, workspace, age_days=400)

        report = prune_workspace(
            conn, workspace, policy=RetentionPolicy(keep_recent_snapshots=0), dry_run=False
        )

        assert not directory.exists()
        assert store(report, STORE_GLOBAL_SNAPSHOTS).reclaimed == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()[
                0
            ]
            == 0
        )

    def test_the_audit_row_keeps_naming_its_reclaimed_snapshot(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        """An applied edit was snapshot-protected whether or not the bytes survive."""
        snapshot_id, _ = aged_snapshot(conn, project_root, project, age_days=400)
        operation_id = record_operation(
            conn,
            project_key=project,
            operation_type="apply_patch",
            target_path="canvases/plan.md",
            snapshot_id=snapshot_id,
            state=OP_APPLIED,
        )

        prune_workspace(
            conn, workspace, policy=RetentionPolicy(keep_recent_snapshots=0), dry_run=False
        )

        row = conn.execute(
            "SELECT snapshot_id FROM operations WHERE id = ?", (operation_id,)
        ).fetchone()
        assert row["snapshot_id"] == snapshot_id


# ── Blobs ────────────────────────────────────────────────────────────────────


class TestBlobSweep:
    def test_a_blob_a_live_file_still_holds_is_kept(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project_root: Path
    ) -> None:
        from ferumind.core.blob_store import adopt_file

        library = project_root / "library"
        library.mkdir(parents=True, exist_ok=True)
        held = library / "photo.bin"
        held.write_bytes(b"still referenced by a live file")
        reference = adopt_file(blob_store_root(project_root), held)

        prune_workspace(
            conn, workspace, policy=RetentionPolicy(keep_recent_snapshots=0), dry_run=False
        )

        remaining = {path.name for path in stored_blobs(blob_store_root(project_root))}
        assert reference.digest in remaining
        assert held.read_bytes() == b"still referenced by a live file"

    def test_the_blob_behind_a_reclaimed_snapshot_goes(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        aged_snapshot(conn, project_root, project, age_days=400, content="only in the snapshot\n")
        store_root = blob_store_root(project_root)
        assert stored_blobs(store_root)

        report = prune_workspace(
            conn, workspace, policy=RetentionPolicy(keep_recent_snapshots=0), dry_run=False
        )

        assert stored_blobs(store_root) == []
        assert store(report, STORE_BLOBS, project).bytes_reclaimed > 0


# ── Operation log ────────────────────────────────────────────────────────────


class TestOperationLog:
    def test_scrubbing_a_diff_keeps_every_other_column(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        operation_id = aged_operation(conn, project, age_days=400)
        before = dict(
            conn.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
        )

        prune_workspace(conn, workspace, dry_run=False)

        after = dict(
            conn.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
        )
        assert after["diff_text"] is None
        assert {key: value for key, value in after.items() if key != "diff_text"} == {
            key: value for key, value in before.items() if key != "diff_text"
        }

    def test_a_recent_diff_survives(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        operation_id = aged_operation(conn, project, age_days=2)

        prune_workspace(conn, workspace, dry_run=False)

        assert (
            conn.execute(
                "SELECT diff_text FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()[0]
            is not None
        )

    def test_spent_proposals_go_and_failed_rows_stay(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        discarded = aged_operation(
            conn,
            project,
            age_days=30,
            state=OP_DISCARDED,
            operation_type="propose_exact_replace_patch",
            diff_text=None,
        )
        failed = aged_operation(
            conn, project, age_days=30, state=OP_FAILED, operation_type="apply_patch"
        )

        report = prune_workspace(conn, workspace, dry_run=False)

        assert store(report, STORE_SPENT_PROPOSALS).reclaimed == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM operations WHERE id = ?", (discarded,)).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM operations WHERE id = ?", (failed,)).fetchone()[0]
            == 1
        )

    def test_the_prune_itself_is_logged_with_counts_only(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        aged_snapshot(conn, project_root, project, age_days=400)

        report = prune_workspace(
            conn, workspace, policy=RetentionPolicy(keep_recent_snapshots=0), dry_run=False
        )

        assert report.operation_id is not None
        row = conn.execute(
            "SELECT operation_type, project_key, source, diff_text, request_json "
            "FROM operations WHERE id = ?",
            (report.operation_id,),
        ).fetchone()
        assert row["operation_type"] == retention.PRUNE_OPERATION_TYPE
        assert row["project_key"] == "__workspace__"
        assert row["source"] == "cli"
        assert row["diff_text"] is None
        assert "reclaimed" in row["request_json"]


# ── Observations, backups, runtime log ───────────────────────────────────────


class TestWorkspaceStores:
    @pytest.mark.usefixtures("project")
    def test_old_observations_go_and_recent_ones_stay(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot
    ) -> None:
        old = aged_observation(conn, age_days=400)
        recent = aged_observation(conn, age_days=1)

        prune_workspace(conn, workspace, dry_run=False)

        assert (
            conn.execute(
                "SELECT COUNT(*) FROM mcp_call_observations WHERE id = ?", (old,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM mcp_call_observations WHERE id = ?", (recent,)
            ).fetchone()[0]
            == 1
        )

    @pytest.mark.usefixtures("project")
    def test_migration_backups_keep_the_newest_n(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot
    ) -> None:
        stamps = ["20250101T000000000000", "20250601T000000000000", "20260101T000000000000"]
        made = [backup_tarball(workspace, stamp) for stamp in stamps]

        report = prune_workspace(
            conn, workspace, policy=RetentionPolicy(keep_migration_backups=2), dry_run=False
        )

        assert [path.is_file() for path in made] == [False, True, True]
        assert store(report, STORE_MIGRATION_BACKUPS).bytes_reclaimed == 4096

    @pytest.mark.usefixtures("project")
    def test_an_oversized_runtime_log_rotates_once(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot
    ) -> None:
        log = contained_path(workspace, RUNTIME_LOG_RELATIVE_PATH)
        log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        log.write_bytes(b"{}\n" * 40_000)

        report = prune_workspace(
            conn, workspace, policy=RetentionPolicy(runtime_log_max_bytes=64 * 1024), dry_run=False
        )

        assert not log.exists()
        assert log.with_name(f"{log.name}.1").is_file()
        assert store(report, STORE_RUNTIME_LOG).reclaimed == 1

    @pytest.mark.usefixtures("project")
    def test_a_small_runtime_log_is_left_alone(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot
    ) -> None:
        log = contained_path(workspace, RUNTIME_LOG_RELATIVE_PATH)
        log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        log.write_bytes(b'{"event": "process_started"}\n')

        report = prune_workspace(conn, workspace, dry_run=False)

        assert log.is_file()
        assert store(report, STORE_RUNTIME_LOG).reclaimed == 0


# ── Database compaction ──────────────────────────────────────────────────────


class TestDatabaseCompaction:
    def test_vacuum_returns_the_bytes(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        for index in range(200):
            aged_operation(conn, project, age_days=400, diff_text=f"{index}" + "d" * 8192)

        report = prune_workspace(conn, workspace, dry_run=False)

        assert report.vacuumed is True
        assert report.database_bytes_after < report.database_bytes_before
        assert store(report, STORE_OPERATION_DIFFS).database_bytes_freed > 1_000_000

    def test_it_refuses_when_vacuum_would_not_fit(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        project_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, directory = aged_snapshot(conn, project_root, project, age_days=400)

        def full(_path: Path) -> _FullDisk:
            return _FullDisk()

        # Patched on ``shutil`` itself: retention imports the module, not the
        # name, so this is the same object it will call.
        monkeypatch.setattr(shutil, "disk_usage", full)

        with pytest.raises(RetentionPrerequisiteError):
            prune_workspace(
                conn, workspace, policy=RetentionPolicy(keep_recent_snapshots=0), dry_run=False
            )

        assert directory.is_dir(), "a refusal must leave the workspace untouched"

    @pytest.mark.usefixtures("project")
    def test_a_dry_run_never_checks_disk_space(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def refuse(_path: Path) -> object:
            raise AssertionError("a dry run must not need headroom it will not use")

        monkeypatch.setattr(shutil, "disk_usage", refuse)
        assert prune_workspace(conn, workspace).dry_run is True


# ── Repeatability and scoping ────────────────────────────────────────────────


class TestRepeatability:
    def test_a_second_run_finds_nothing_left(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        aged_snapshot(conn, project_root, project, age_days=400)
        aged_operation(conn, project, age_days=400)
        aged_observation(conn, age_days=400)
        backup_tarball(workspace, "20200101T000000000000")
        backup_tarball(workspace, "20260101T000000000001")
        backup_tarball(workspace, "20260102T000000000002")
        policy = RetentionPolicy(keep_recent_snapshots=0)

        first = prune_workspace(conn, workspace, policy=policy, dry_run=False)
        second = prune_workspace(conn, workspace, policy=policy, dry_run=False)

        assert first.total_reclaimed > 0
        assert second.total_reclaimed == 0

    def test_an_interrupted_removal_is_finished_by_the_next_run(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        """A directory removed without its row is the crash window; it must close."""
        snapshot_id, directory = aged_snapshot(conn, project_root, project, age_days=400)
        shutil.rmtree(directory)

        prune_workspace(
            conn, workspace, policy=RetentionPolicy(keep_recent_snapshots=0), dry_run=False
        )

        assert (
            conn.execute("SELECT COUNT(*) FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()[
                0
            ]
            == 0
        )


class TestScoping:
    def test_project_scope_leaves_workspace_stores_alone(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        _, snapshot_dir = aged_snapshot(conn, project_root, project, age_days=400)
        observation_id = aged_observation(conn, age_days=400)
        doomed_backup = backup_tarball(workspace, "20200101T000000000000")
        backup_tarball(workspace, "20260101T000000000001")
        backup_tarball(workspace, "20260102T000000000002")

        report = prune_workspace(
            conn,
            workspace,
            policy=RetentionPolicy(keep_recent_snapshots=0),
            project=project,
            dry_run=False,
        )

        assert not snapshot_dir.exists()
        assert doomed_backup.is_file()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM mcp_call_observations WHERE id = ?", (observation_id,)
            ).fetchone()[0]
            == 1
        )
        assert not any(entry.store == STORE_OBSERVATIONS for entry in report.stores)

    @pytest.mark.usefixtures("project")
    def test_an_unknown_project_is_refused(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot
    ) -> None:
        with pytest.raises(ProjectNotFoundError):
            prune_workspace(conn, workspace, project="no-such-project")


class TestPolicy:
    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("snapshot_max_age_days", 0),
            ("keep_recent_snapshots", -1),
            ("keep_migration_backups", 0),
            ("runtime_log_max_bytes", 1),
        ],
    )
    def test_out_of_range_values_are_refused(self, field_name: str, value: int) -> None:
        with pytest.raises(PydanticValidationError):
            RetentionPolicy.model_validate({field_name: value})

    def test_defaults_are_the_documented_local_ones(self) -> None:
        policy = RetentionPolicy()
        assert policy.snapshot_max_age_days == 180
        assert policy.diff_scrub_age_days == 30
        assert policy.observation_max_age_days == 30
        assert policy.keep_migration_backups == 2
        assert policy.runtime_log_max_bytes == 8 * 1024 * 1024


def test_no_mcp_tool_can_prune() -> None:
    """An agent must not be able to reclaim the user's history (STORE-03)."""
    from ferumind.mcp import server
    from ferumind.mcp.sdk_internals import registered_tools

    server.register_all_tools()
    names = {tool.name for tool in registered_tools(server.mcp)}
    assert not {name for name in names if "prune" in name or "retention" in name}

    mcp_dir = Path(server.__file__).parent
    assert not any(
        "retention" in path.read_text(encoding="utf-8") for path in mcp_dir.rglob("*.py")
    ), "core.retention must not be reachable from the MCP layer"


class TestTheReportSaysWhatIsThere:
    """``examined`` counts the whole store, not the slice past the cutoff.

    The first live run reported ``operation_diffs: 0 of 0`` against 2,411
    retained diffs and ``observations: 0 of 0`` against 7,236 rows, because
    both counted only what the window had already caught. An operator reads
    that as an empty store and stops asking whether a shorter window is worth
    passing.
    """

    def test_retained_diffs_are_counted_even_when_none_expired(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        for _ in range(3):
            aged_operation(conn, project, age_days=1)

        report = prune_workspace(conn, workspace)

        entry = store(report, STORE_OPERATION_DIFFS)
        assert entry.examined == 3, "the diffs are there and must be counted"
        assert entry.reclaimed == 0, "none is past the window"

    @pytest.mark.usefixtures("project")
    def test_observations_are_counted_even_when_none_expired(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot
    ) -> None:
        for _ in range(4):
            aged_observation(conn, age_days=1)

        report = prune_workspace(conn, workspace)

        entry = store(report, STORE_OBSERVATIONS)
        assert entry.examined == 4
        assert entry.reclaimed == 0

    def test_spent_proposals_are_counted_even_when_none_expired(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        aged_operation(
            conn,
            project,
            age_days=1,
            state=OP_DISCARDED,
            operation_type="propose_exact_replace_patch",
            diff_text=None,
        )

        report = prune_workspace(conn, workspace)

        entry = store(report, STORE_SPENT_PROPOSALS)
        assert entry.examined == 1
        assert entry.reclaimed == 0

    def test_the_split_between_held_and_taken_is_visible(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        """The number that matters: how much of what is there would go."""
        for _ in range(2):
            aged_operation(conn, project, age_days=400)
        for _ in range(5):
            aged_operation(conn, project, age_days=1)

        report = prune_workspace(conn, workspace)

        entry = store(report, STORE_OPERATION_DIFFS)
        assert (entry.examined, entry.reclaimed) == (7, 2)

    def test_a_project_scoped_run_counts_only_that_project(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        aged_operation(conn, project, age_days=1)
        aged_operation(conn, "some-other-project", age_days=1)

        report = prune_workspace(conn, workspace, project=project)

        assert store(report, STORE_OPERATION_DIFFS).examined == 1


class TestTheSummaryRollup:
    """``by_store`` is what the default CLI view prints.

    Thirteen projects make the per-project listing unreadable, and filtering
    the empty rows out instead made the default view claim the workspace was
    empty. Rolling up keeps every store visible in a handful of lines.
    """

    def test_projects_collapse_into_one_row_per_store(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        aged_snapshot(conn, project_root, project, age_days=400)

        report = prune_workspace(conn, workspace, policy=RetentionPolicy(keep_recent_snapshots=0))

        names = [entry.store for entry in report.by_store()]
        assert len(names) == len(set(names)), "each store appears once"
        assert STORE_SNAPSHOTS in names

    def test_counts_are_summed_not_dropped(self) -> None:
        rolled = PruneReport(
            dry_run=True,
            policy=RetentionPolicy(),
            stores=[
                retention.StoreReclaim(
                    store=STORE_SNAPSHOTS, scope="a", examined=10, reclaimed=2, bytes_reclaimed=100
                ),
                retention.StoreReclaim(
                    store=STORE_SNAPSHOTS, scope="b", examined=5, reclaimed=1, bytes_reclaimed=50
                ),
            ],
            projects=["a", "b"],
            database_bytes_before=0,
            database_bytes_after=0,
            vacuumed=False,
        ).by_store()

        assert len(rolled) == 1
        assert (rolled[0].examined, rolled[0].reclaimed, rolled[0].bytes_reclaimed) == (15, 3, 150)

    def test_a_store_with_nothing_to_take_still_appears(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        """The regression that made a full workspace read as an empty one."""
        for _ in range(3):
            aged_operation(conn, project, age_days=1)

        rolled = {entry.store: entry for entry in prune_workspace(conn, workspace).by_store()}

        assert rolled[STORE_OPERATION_DIFFS].reclaimed == 0
        assert rolled[STORE_OPERATION_DIFFS].examined == 3


class TestTheReportExplainsItself:
    """A store holding rows it will not give up has to say why.

    Three rounds of "it keeps saying there is nothing to prune" came down to
    windows longer than the workspace was old. The report knows both numbers;
    withholding them made a working command look broken.
    """

    def test_a_held_store_reports_its_oldest_item_and_its_window(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        aged_operation(conn, project, age_days=5)

        entry = store(prune_workspace(conn, workspace), STORE_OPERATION_DIFFS)

        assert entry.reclaimed == 0
        assert entry.oldest_age_days == 5
        assert entry.window_days == RetentionPolicy().diff_scrub_age_days

    def test_snapshots_report_the_age_of_the_oldest_directory(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        aged_snapshot(conn, project_root, project, age_days=3, content="newer\n")
        aged_snapshot(conn, project_root, project, age_days=40, content="older\n")

        entry = store(prune_workspace(conn, workspace), STORE_SNAPSHOTS, project)

        assert entry.oldest_age_days == 40
        assert entry.window_days == RetentionPolicy().snapshot_max_age_days

    @pytest.mark.usefixtures("project")
    def test_an_empty_store_claims_no_age(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot
    ) -> None:
        entry = store(prune_workspace(conn, workspace), STORE_OBSERVATIONS)

        assert entry.examined == 0
        assert entry.oldest_age_days is None

    def test_the_rollup_keeps_the_oldest_across_projects(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, project_root: Path
    ) -> None:
        aged_snapshot(conn, project_root, project, age_days=12)

        rolled = {e.store: e for e in prune_workspace(conn, workspace).by_store()}

        assert rolled[STORE_SNAPSHOTS].oldest_age_days == 12
        assert rolled[STORE_SNAPSHOTS].window_days == RetentionPolicy().snapshot_max_age_days
