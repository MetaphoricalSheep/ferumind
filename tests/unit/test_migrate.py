"""Tests for the workspace migration frame (spec-versioning §1.3)."""

from __future__ import annotations

import sqlite3
import tarfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from lattice.core import migrate as migrate_module
from lattice.core.errors import FormatUnsupportedError
from lattice.core.format import read_format, write_format_marker
from lattice.core.migrate import MIGRATORS, plan_migration, run_migration
from lattice.core.operations import list_operations
from lattice.core.paths import PathSafetyError, WorkspaceRoot


def test_shipped_migrator_registry_is_empty() -> None:
    # v2 ships the frame, not a migration; the first real migrator arrives
    # with the first v3-breaking change (§1.4).
    assert MIGRATORS == {}


def test_plan_noop_when_already_current(workspace: WorkspaceRoot) -> None:
    plan = plan_migration(workspace)
    assert plan.steps == []
    assert plan.from_format == plan.to_format == 2


def test_plan_fails_without_a_migrator_path(workspace: WorkspaceRoot) -> None:
    write_format_marker(workspace, 1)
    with pytest.raises(FormatUnsupportedError, match="No migrator registered"):
        plan_migration(workspace, migrators={})


def test_plan_refuses_downgrade(workspace: WorkspaceRoot) -> None:
    write_format_marker(workspace, 3)
    with pytest.raises(FormatUnsupportedError, match="newer"):
        plan_migration(workspace, target_format=2, migrators={})


def test_missing_marker_plans_from_format_1(workspace: WorkspaceRoot) -> None:
    (workspace / "system" / "meta.yml").unlink()
    plan = plan_migration(workspace, migrators={1: lambda _ws: None})
    assert plan.from_format == 1
    assert plan.steps == [1]


def test_dry_run_writes_nothing(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    write_format_marker(workspace, 1)
    calls: list[int] = []
    report = run_migration(
        conn, workspace, dry_run=True, migrators={1: lambda _ws: calls.append(1)}
    )
    assert report.dry_run
    assert report.backup_path is None
    assert calls == []
    assert read_format(workspace) == 1


def test_run_migration_replans_after_acquiring_workspace_lock(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_format_marker(workspace, 1)
    calls: list[int] = []

    @contextmanager
    def competing_migration(_workspace: WorkspaceRoot) -> Generator[None]:
        write_format_marker(workspace, 2)
        yield

    monkeypatch.setattr(migrate_module, "acquire_workspace_lock", competing_migration)
    report = run_migration(
        conn,
        workspace,
        migrators={1: lambda _ws: calls.append(1)},
    )

    assert report.plan.from_format == 2
    assert report.plan.steps == []
    assert calls == []


def test_run_migration_backs_up_migrates_and_bumps(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    write_format_marker(workspace, 1)
    backup_dir = workspace / ".lattice/test-backups"

    def fake_migrator(ws: WorkspaceRoot) -> None:
        (ws / "system" / "migrated.txt").write_text("done", encoding="utf-8")

    report = run_migration(conn, workspace, migrators={1: fake_migrator}, backup_dir=backup_dir)
    assert not report.dry_run
    assert read_format(workspace) == 2
    assert (workspace / "system" / "migrated.txt").read_text(encoding="utf-8") == "done"
    assert report.reindexed_documents >= 2  # spine + seeded rules

    # Backup is a full workspace tarball taken before the migrators ran.
    assert report.backup_path is not None
    with tarfile.open(report.backup_path) as tar:
        names = tar.getnames()
    assert "workspace/system/meta.yml" in names
    assert "workspace/system/migrated.txt" not in names
    assert "workspace/.lattice/lattice.sqlite" in names

    ops = [op for op in list_operations(conn, project) if op.operation_type == "migrate"]
    assert ops
    assert ops[0].snapshot_id == report.snapshot_id


def test_failed_migration_requires_backup_recovery_before_retry(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    write_format_marker(workspace, 1)

    def failing_migrator(ws: WorkspaceRoot) -> None:
        (ws / "system" / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected migration failure"):
        run_migration(conn, workspace, migrators={1: failing_migrator})

    marker = workspace / migrate_module.MIGRATION_FAILURE_MARKER
    assert marker.is_file()
    assert (workspace / "system" / "partial.txt").is_file()
    assert read_format(workspace) == 1
    with pytest.raises(FormatUnsupportedError, match="restore"):
        run_migration(conn, workspace, migrators={1: lambda _ws: None})


def test_default_backup_is_private_internal_and_does_not_archive_itself(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    write_format_marker(workspace, 1)
    report = run_migration(conn, workspace, migrators={1: lambda _ws: None})

    assert report.backup_path is not None
    backup = Path(report.backup_path)
    assert backup.is_relative_to(workspace / ".lattice/backups")
    assert backup.is_file()
    assert backup.stat().st_mode & 0o777 == 0o600
    with tarfile.open(backup) as archive:
        assert not any(name.startswith("workspace/.lattice/backups") for name in archive.getnames())


def test_migration_refuses_backup_outside_workspace(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    write_format_marker(workspace, 1)
    outside = workspace.parent / "outside-backups"

    with pytest.raises(PathSafetyError, match="inside the workspace"):
        run_migration(
            conn,
            workspace,
            migrators={1: lambda _ws: None},
            backup_dir=outside,
        )

    assert not outside.exists()


def test_migration_refuses_workspace_root_as_backup_directory(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    write_format_marker(workspace, 1)
    with pytest.raises(PathSafetyError, match="dedicated"):
        run_migration(
            conn,
            workspace,
            migrators={1: lambda _ws: None},
            backup_dir=workspace,
        )
