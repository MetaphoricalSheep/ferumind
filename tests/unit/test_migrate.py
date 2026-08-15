"""Tests for the workspace migration frame (spec-versioning §1.3)."""

from __future__ import annotations

import sqlite3
import tarfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from ferumind.core import migrate as migrate_module
from ferumind.core.errors import FormatUnsupportedError, MigrationPrerequisiteError
from ferumind.core.format import SUPPORTED_FORMAT, read_format, write_format_marker
from ferumind.core.indexer import IndexResult
from ferumind.core.migrate import MIGRATORS, PREFLIGHTS, plan_migration, run_migration
from ferumind.core.operations import list_operations
from ferumind.core.paths import PathSafetyError, WorkspaceRoot


def test_migration_registries_are_empty_between_format_bumps() -> None:
    assert MIGRATORS == {}
    assert PREFLIGHTS == {}


def test_plan_noop_when_already_current(workspace: WorkspaceRoot) -> None:
    plan = plan_migration(workspace)
    assert plan.steps == []
    assert plan.from_format == plan.to_format == SUPPORTED_FORMAT


def test_plan_fails_without_a_migrator_path(workspace: WorkspaceRoot) -> None:
    """Format 1 is the floor, so the gap is proved against a synthetic target.

    ``target_format`` is explicit rather than defaulted because there is no
    format below the supported one to migrate *from* — the reachable failure
    is a bump whose migrator was never registered.
    """
    write_format_marker(workspace, 1)
    with pytest.raises(FormatUnsupportedError, match="No migrator registered for format 1 → 2"):
        plan_migration(workspace, target_format=2, migrators={})


def test_plan_refuses_downgrade(workspace: WorkspaceRoot) -> None:
    write_format_marker(workspace, 2)
    with pytest.raises(FormatUnsupportedError, match="newer"):
        plan_migration(workspace, target_format=1, migrators={})


def test_missing_marker_refuses_to_invent_a_starting_format(workspace: WorkspaceRoot) -> None:
    """An unmarked directory has no format, so there is nothing to migrate.

    Substituting a number here would plan a phantom chain and report progress
    against a starting point the workspace never claimed.
    """
    (workspace / "system" / "meta.yml").unlink()
    with pytest.raises(FormatUnsupportedError, match="no starting format"):
        plan_migration(workspace, migrators={1: lambda _ws: None})


def test_dry_run_writes_nothing(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    write_format_marker(workspace, 1)
    calls: list[int] = []
    report = run_migration(
        conn,
        workspace,
        dry_run=True,
        target_format=2,
        migrators={1: lambda _ws: calls.append(1)},
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
        target_format=2,
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
    backup_dir = workspace / ".ferumind/test-backups"

    def fake_migrator(ws: WorkspaceRoot) -> None:
        (ws / "system" / "migrated.txt").write_text("done", encoding="utf-8")

    report = run_migration(
        conn,
        workspace,
        target_format=2,
        migrators={1: fake_migrator},
        backup_dir=backup_dir,
    )
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
    assert "workspace/.ferumind/ferumind.sqlite" in names

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
        run_migration(
            conn,
            workspace,
            target_format=2,
            migrators={1: failing_migrator},
        )

    marker = workspace / migrate_module.MIGRATION_FAILURE_MARKER
    assert marker.is_file()
    failed_state = migrate_module.MigrationRecoveryState.model_validate_json(
        marker.read_text(encoding="utf-8")
    )
    assert failed_state.state == "failed"
    assert failed_state.from_format == 1
    assert failed_state.to_format == 2
    assert failed_state.backup_path
    assert failed_state.snapshot_id
    assert (workspace / "system" / "partial.txt").is_file()
    assert read_format(workspace) == 1
    with pytest.raises(FormatUnsupportedError, match="restore"):
        run_migration(
            conn,
            workspace,
            target_format=2,
            migrators={1: lambda _ws: None},
        )


def test_recovery_marker_is_durable_before_transform_runs(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    write_format_marker(workspace, 1)
    observed_states: list[str] = []

    def inspect_armed_state(ws: WorkspaceRoot) -> None:
        marker = ws / migrate_module.MIGRATION_FAILURE_MARKER
        state = migrate_module.MigrationRecoveryState.model_validate_json(
            marker.read_text(encoding="utf-8")
        )
        observed_states.append(state.state)
        assert state.backup_path
        assert state.snapshot_id

    run_migration(
        conn,
        workspace,
        target_format=2,
        migrators={1: inspect_armed_state},
    )

    assert observed_states == ["transforming"]
    assert read_format(workspace) == 2
    assert not (workspace / migrate_module.MIGRATION_FAILURE_MARKER).exists()


def test_recovery_marker_fsync_failure_stops_before_transform(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project
    write_format_marker(workspace, 1)
    migrator_called = False

    def visible_then_failed(target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        raise OSError("injected recovery-marker directory fsync failure")

    def migrator_must_not_run(_workspace: WorkspaceRoot) -> None:
        nonlocal migrator_called
        migrator_called = True

    monkeypatch.setattr(migrate_module, "atomic_write_text", visible_then_failed)

    with pytest.raises(OSError, match="recovery-marker directory fsync failure"):
        run_migration(
            conn,
            workspace,
            target_format=2,
            migrators={1: migrator_must_not_run},
        )

    assert not migrator_called
    assert read_format(workspace) == 1


def test_persisted_transforming_state_blocks_replay(
    workspace: WorkspaceRoot,
) -> None:
    write_format_marker(workspace, 1)
    marker = workspace / migrate_module.MIGRATION_FAILURE_MARKER
    state = migrate_module.MigrationRecoveryState(
        state="transforming",
        from_format=1,
        to_format=2,
        backup_path=str(workspace / ".ferumind/backups/backup.tar.gz"),
        snapshot_id="snapshot-id",
        started_at="2026-08-13T00:00:00+00:00",
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")

    with pytest.raises(FormatUnsupportedError, match="restore"):
        plan_migration(workspace, target_format=2, migrators={1: lambda _ws: None})


def test_audit_committed_recovery_state_only_allows_published_target(
    workspace: WorkspaceRoot,
) -> None:
    write_format_marker(workspace, 1)
    marker = workspace / migrate_module.MIGRATION_FAILURE_MARKER
    state = migrate_module.MigrationRecoveryState(
        state="audit_committed",
        from_format=1,
        to_format=2,
        backup_path=str(workspace / ".ferumind/backups/backup.tar.gz"),
        snapshot_id="snapshot-id",
        started_at="2026-08-13T00:00:00+00:00",
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")

    with pytest.raises(FormatUnsupportedError, match="restore"):
        plan_migration(workspace, target_format=2, migrators={1: lambda _ws: None})

    write_format_marker(workspace, 2)
    plan = plan_migration(workspace, target_format=2, migrators={})
    assert plan.steps == []
    assert plan.from_format == plan.to_format == 2


def test_preflight_failure_runs_under_all_locks_and_changes_nothing(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ferumind.core.registry import ProjectEntry, load_registry, save_registry

    write_format_marker(workspace, 1)
    registry = load_registry(workspace)
    registry["other"] = ProjectEntry(
        key="other",
        title="Other",
        path="projects/other",
        status="active",
    )
    save_registry(workspace, registry)
    (workspace / "projects/other").mkdir()
    other_lock = workspace / "projects/other/.ferumind/locks/other.lock"
    other_lock.parent.mkdir(parents=True)
    other_lock.touch()
    before = {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    lock_events: list[str] = []

    @contextmanager
    def workspace_lock(_workspace: WorkspaceRoot) -> Generator[None]:
        lock_events.append("workspace-enter")
        try:
            yield
        finally:
            lock_events.append("workspace-exit")

    @contextmanager
    def project_lock(
        _project_dir: Path,
        project_key: str,
    ) -> Generator[None]:
        lock_events.append(f"project-enter:{project_key}")
        try:
            yield
        finally:
            lock_events.append(f"project-exit:{project_key}")

    def failing_preflight(_workspace: WorkspaceRoot) -> list[str]:
        assert lock_events == [
            "workspace-enter",
            f"project-enter:{project}",
            "project-enter:other",
        ]
        return ["one document is missing its description"]

    monkeypatch.setattr(migrate_module, "acquire_workspace_lock", workspace_lock)
    monkeypatch.setattr(migrate_module, "acquire_project_lock", project_lock)

    with pytest.raises(MigrationPrerequisiteError, match="missing its description"):
        run_migration(
            conn,
            workspace,
            target_format=2,
            migrators={1: lambda _ws: None},
            preflights={1: failing_preflight},
        )

    after = {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert not (workspace / migrate_module.MIGRATION_FAILURE_MARKER).exists()
    assert not (workspace / ".ferumind/backups").exists()


def test_missing_registered_project_fails_before_backup(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    from ferumind.core.registry import ProjectEntry, load_registry, save_registry

    write_format_marker(workspace, 1)
    registry = load_registry(workspace)
    registry["missing"] = ProjectEntry(
        key="missing",
        title="Missing",
        path="projects/missing",
        status="active",
    )
    save_registry(workspace, registry)

    with pytest.raises(MigrationPrerequisiteError, match="no project directory"):
        run_migration(
            conn,
            workspace,
            target_format=2,
            migrators={1: lambda _ws: None},
        )

    assert read_format(workspace) == 1
    assert not (workspace / migrate_module.MIGRATION_FAILURE_MARKER).exists()
    assert not (workspace / ".ferumind/backups").exists()


def test_rebuild_error_prevents_format_stamp_and_requires_recovery(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_format_marker(workspace, 1)

    def failed_rebuild(
        _conn: sqlite3.Connection,
        _workspace_root: Path,
        _project_keys: list[str],
        *,
        locks_held: bool = False,
    ) -> IndexResult:
        del locks_held
        return IndexResult(
            errors=1,
            error_messages=["canvases/broken.md: indexing failed (ValueError)"],
        )

    monkeypatch.setattr(
        migrate_module,
        "rebuild_index",
        failed_rebuild,
    )

    with pytest.raises(migrate_module.MigrationExecutionError, match="index rebuild failed"):
        run_migration(
            conn,
            workspace,
            target_format=2,
            migrators={1: lambda _ws: None},
        )

    assert read_format(workspace) == 1
    assert (workspace / migrate_module.MIGRATION_FAILURE_MARKER).is_file()


def test_audit_failure_prevents_format_stamp_and_requires_recovery(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_format_marker(workspace, 1)

    def failed_audit(*_args: object, **_kwargs: object) -> str:
        raise sqlite3.OperationalError("injected audit failure")

    monkeypatch.setattr(migrate_module, "record_operation", failed_audit)

    with pytest.raises(sqlite3.OperationalError, match="injected audit failure"):
        run_migration(
            conn,
            workspace,
            target_format=2,
            migrators={1: lambda _ws: None},
        )

    assert read_format(workspace) == 1
    assert (workspace / migrate_module.MIGRATION_FAILURE_MARKER).is_file()


def test_post_replace_marker_error_treats_visible_target_as_committed(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_format_marker(workspace, 1)
    publish_calls: list[int] = []

    def published_then_failed(ws: WorkspaceRoot, target_format: int) -> None:
        publish_calls.append(target_format)
        write_format_marker(ws, target_format)
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr(migrate_module, "write_format_marker", published_then_failed)

    report = run_migration(
        conn,
        workspace,
        target_format=2,
        migrators={1: lambda _ws: None},
    )

    assert publish_calls == [2], "the injected publication failure never fired"
    assert report.plan.to_format == 2
    assert read_format(workspace) == 2
    assert not (workspace / migrate_module.MIGRATION_FAILURE_MARKER).exists()
    assert any(op.operation_type == "migrate" for op in list_operations(conn, project))


def test_uncertain_format_publication_retains_guard_and_blocks_replay(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del project
    write_format_marker(workspace, 1)

    def published_then_failed(ws: WorkspaceRoot, target_format: int) -> None:
        write_format_marker(ws, target_format)
        raise OSError("injected format-marker directory fsync failure")

    def failed_retry(_directory: Path) -> None:
        raise OSError("injected retry fsync failure")

    monkeypatch.setattr(migrate_module, "write_format_marker", published_then_failed)
    monkeypatch.setattr(migrate_module, "_fsync_directory", failed_retry)

    report = run_migration(
        conn,
        workspace,
        target_format=2,
        migrators={1: lambda _ws: None},
    )

    assert report.plan.to_format == 2
    assert read_format(workspace) == 2
    marker = workspace / migrate_module.MIGRATION_FAILURE_MARKER
    state = migrate_module.MigrationRecoveryState.model_validate_json(
        marker.read_text(encoding="utf-8")
    )
    assert state.state == "audit_committed"

    # Simulate a reboot losing the un-fsynced format-marker replacement. The
    # separately durable replay guard still makes an in-place retry impossible.
    write_format_marker(workspace, 1)
    with pytest.raises(FormatUnsupportedError, match="restore"):
        plan_migration(workspace, target_format=2, migrators={1: lambda _ws: None})


def test_default_backup_is_private_internal_and_does_not_archive_itself(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    write_format_marker(workspace, 1)
    report = run_migration(
        conn,
        workspace,
        target_format=2,
        migrators={1: lambda _ws: None},
    )

    assert report.backup_path is not None
    backup = Path(report.backup_path)
    assert backup.is_relative_to(workspace / ".ferumind/backups")
    assert backup.is_file()
    assert backup.stat().st_mode & 0o777 == 0o600
    with tarfile.open(backup) as archive:
        assert not any(
            name.startswith("workspace/.ferumind/backups") for name in archive.getnames()
        )


def test_backup_contains_transactionally_consistent_database(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    tmp_path: Path,
) -> None:
    write_format_marker(workspace, 1)
    conn.execute("CREATE TABLE migration_backup_probe (value TEXT NOT NULL)")
    conn.execute("INSERT INTO migration_backup_probe VALUES ('committed')")
    conn.commit()

    report = run_migration(
        conn,
        workspace,
        target_format=2,
        migrators={1: lambda _ws: None},
    )

    assert report.backup_path is not None
    extracted_database = tmp_path / "backed-up.sqlite"
    with tarfile.open(report.backup_path) as archive:
        database_member = archive.extractfile("workspace/.ferumind/ferumind.sqlite")
        assert database_member is not None
        extracted_database.write_bytes(database_member.read())
        assert "workspace/.ferumind/ferumind.sqlite-wal" not in archive.getnames()
        assert "workspace/.ferumind/ferumind.sqlite-shm" not in archive.getnames()
    restored = sqlite3.connect(extracted_database)
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert restored.execute("SELECT value FROM migration_backup_probe").fetchone() == (
            "committed",
        )
    finally:
        restored.close()


def test_migration_refuses_backup_outside_workspace(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    write_format_marker(workspace, 1)
    outside = workspace.parent / "outside-backups"

    with pytest.raises(PathSafetyError, match="inside the workspace"):
        run_migration(
            conn,
            workspace,
            target_format=2,
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
            target_format=2,
            migrators={1: lambda _ws: None},
            backup_dir=workspace,
        )
