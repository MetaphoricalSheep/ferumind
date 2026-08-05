"""Workspace format migration frame (product/spec-versioning.md §1.3).

Migration of user Markdown is never implicit: ``ferumind migrate`` is the
only entry point. The v2 build ships the frame with an **empty** migrator
registry — the first real migrator arrives with the first v3-breaking
change, and the standing rule (§1.4) guarantees it ships in the same change
that breaks the format.

Flow: resolve the ``N → N+1`` migrator chain, create a full workspace
tarball backup and a global snapshot, run each migrator, bump ``meta.yml``,
trigger a full reindex, and write an operation-log entry per project.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tarfile
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ferumind.core.errors import FormatUnsupportedError
from ferumind.core.file_io import atomic_write_text
from ferumind.core.format import SUPPORTED_FORMAT, read_format, write_format_marker
from ferumind.core.indexer import rebuild_index
from ferumind.core.locks import acquire_project_lock, acquire_workspace_lock
from ferumind.core.operations import SOURCE_CLI, record_operation
from ferumind.core.paths import (
    PathSafetyError,
    WorkspaceRoot,
    contained_path,
    contained_project_root,
)
from ferumind.core.registry import load_registry
from ferumind.core.snapshots import (
    create_global_snapshot,
    new_snapshot_id,
    record_snapshot_in_db,
)
from ferumind.core.types import DbConnection

#: A migrator transforms the workspace tree in place from format N to N+1.
type Migrator = Callable[[WorkspaceRoot], None]

#: Registered migrators keyed by their source format (N → N+1).
#: Deliberately empty in the v2 build; tests register fakes.
MIGRATORS: dict[int, Migrator] = {}
MIGRATION_FAILURE_MARKER = ".ferumind/MIGRATION_RECOVERY_REQUIRED.json"


class MigrationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_format: int
    to_format: int
    steps: list[int]


class MigrationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: MigrationPlan
    dry_run: bool
    backup_path: str | None = None
    snapshot_id: str | None = None
    reindexed_documents: int = 0


def plan_migration(
    workspace: WorkspaceRoot,
    *,
    target_format: int = SUPPORTED_FORMAT,
    migrators: dict[int, Migrator] | None = None,
) -> MigrationPlan:
    """Resolve the migrator chain or fail with a clear error."""
    recovery_marker = contained_path(workspace, MIGRATION_FAILURE_MARKER)
    if recovery_marker.is_file():
        raise FormatUnsupportedError(
            "A previous workspace migration failed after making changes; "
            "restore its recorded backup before retrying",
            details={"recovery_marker": MIGRATION_FAILURE_MARKER},
        )
    registry = MIGRATORS if migrators is None else migrators
    found = read_format(workspace)
    current = found if found is not None else 1
    if current == target_format:
        return MigrationPlan(from_format=current, to_format=target_format, steps=[])
    if current > target_format:
        msg = (
            f"Workspace format {current} is newer than this build's format "
            f"{target_format}; upgrade the Ferumind server instead of migrating"
        )
        raise FormatUnsupportedError(msg)
    steps: list[int] = []
    for step in range(current, target_format):
        if step not in registry:
            msg = (
                f"No migrator registered for format {step} → {step + 1}; "
                "cannot migrate this workspace with this build"
            )
            raise FormatUnsupportedError(msg)
        steps.append(step)
    return MigrationPlan(from_format=current, to_format=target_format, steps=steps)


def create_backup_tarball(
    conn: DbConnection,
    workspace: WorkspaceRoot,
    backup_dir: Path,
) -> Path:
    """Create a full, private workspace backup before migrating.

    The live SQLite/WAL files are replaced in the archive by SQLite's online
    backup output, so the durable history is transactionally consistent.
    """
    candidate = backup_dir if backup_dir.is_absolute() else workspace / backup_dir
    try:
        relative = candidate.absolute().relative_to(workspace.absolute())
    except ValueError as exc:
        raise PathSafetyError("Migration backups must stay inside the workspace") from exc
    if relative == Path("."):
        raise PathSafetyError("Migration backups must use a dedicated workspace subdirectory")
    safe_backup_dir = contained_path(workspace, relative.as_posix())
    safe_backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    safe_backup_dir.chmod(0o700)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    backup_path = contained_path(safe_backup_dir, f"workspace-backup-{stamp}.tar.gz")
    rel_backup_dir = safe_backup_dir.relative_to(workspace.resolve()).as_posix()
    excluded_archive_prefix = f"workspace/{rel_backup_dir}"
    live_database_names = {
        "workspace/.ferumind/ferumind.sqlite",
        "workspace/.ferumind/ferumind.sqlite-wal",
        "workspace/.ferumind/ferumind.sqlite-shm",
    }

    def backup_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if (
            info.name == excluded_archive_prefix
            or info.name.startswith(f"{excluded_archive_prefix}/")
            or info.name in live_database_names
        ):
            return None
        return info

    archive_fd, archive_name = tempfile.mkstemp(
        dir=safe_backup_dir,
        prefix=".ferumind-backup-",
        suffix=".tar.gz",
    )
    os.close(archive_fd)
    temporary_archive = Path(archive_name)
    database_fd, database_name = tempfile.mkstemp(
        dir=safe_backup_dir,
        prefix=".ferumind-database-",
        suffix=".sqlite",
    )
    os.close(database_fd)
    consistent_database = Path(database_name)
    try:
        destination = sqlite3.connect(consistent_database)
        try:
            conn.backup(destination)
        finally:
            destination.close()

        with tarfile.open(temporary_archive, "w:gz", dereference=False) as tar:
            tar.add(workspace, arcname="workspace", filter=backup_filter)
            tar.add(
                consistent_database,
                arcname="workspace/.ferumind/ferumind.sqlite",
                recursive=False,
            )
        temporary_archive.chmod(0o600)
        temporary_archive.replace(backup_path)
        directory_fd = os.open(safe_backup_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_archive.unlink(missing_ok=True)
        consistent_database.unlink(missing_ok=True)
    return backup_path


def run_migration(
    conn: DbConnection,
    workspace: WorkspaceRoot,
    *,
    dry_run: bool = False,
    target_format: int = SUPPORTED_FORMAT,
    migrators: dict[int, Migrator] | None = None,
    backup_dir: Path | None = None,
) -> MigrationReport:
    """Run (or plan) an explicit workspace migration."""
    plan = plan_migration(workspace, target_format=target_format, migrators=migrators)
    registry = MIGRATORS if migrators is None else migrators

    if dry_run or not plan.steps:
        return MigrationReport(plan=plan, dry_run=dry_run)

    with acquire_workspace_lock(workspace):
        # The marker may have changed while this process waited for another
        # migrator. Re-plan under the workspace lock so an N→N+1 transform is
        # never replayed against a tree another process already upgraded.
        plan = plan_migration(
            workspace,
            target_format=target_format,
            migrators=registry,
        )
        if not plan.steps:
            return MigrationReport(plan=plan, dry_run=False)
        initial_project_keys = sorted(load_registry(workspace))
        with ExitStack() as project_locks:
            locked_keys: set[str] = set()
            for key in initial_project_keys:
                project_locks.enter_context(
                    acquire_project_lock(contained_project_root(workspace, key), key)
                )
                locked_keys.add(key)

            resolved_backup_dir = (
                backup_dir
                if backup_dir is not None
                else contained_path(workspace, ".ferumind/backups")
            )
            backup_path = create_backup_tarball(conn, workspace, resolved_backup_dir)

            marker_path = contained_path(workspace, "system/meta.yml")
            before_marker = marker_path.read_text(encoding="utf-8") if marker_path.is_file() else ""
            snapshot_id = new_snapshot_id()
            snapshot_dir = create_global_snapshot(
                workspace,
                snapshot_id=snapshot_id,
                operation_type="migrate",
                target_project_key=None,
                reason=f"migrate format {plan.from_format} -> {plan.to_format}",
                before_files={"system/meta.yml": before_marker},
                after_files={},
            )

            transform_started = False
            try:
                transform_started = True
                for step in plan.steps:
                    registry[step](workspace)

                write_format_marker(workspace, plan.to_format)

                project_keys = sorted(load_registry(workspace))
                for key in project_keys:
                    if key not in locked_keys:
                        project_locks.enter_context(
                            acquire_project_lock(contained_project_root(workspace, key), key)
                        )
                        locked_keys.add(key)
                result = rebuild_index(conn, workspace, project_keys, locks_held=True)

                for key in project_keys:
                    record_operation(
                        conn,
                        project_key=key,
                        operation_type="migrate",
                        source=SOURCE_CLI,
                        request_json={
                            "from_format": plan.from_format,
                            "to_format": plan.to_format,
                            "backup_path": str(backup_path),
                        },
                        snapshot_id=snapshot_id,
                        commit=False,
                    )
                record_snapshot_in_db(
                    conn,
                    snapshot_id=snapshot_id,
                    project_key="",
                    target_path=None,
                    snapshot_dir=str(snapshot_dir),
                    reason="migrate",
                    commit=False,
                )
                conn.commit()
            except BaseException as exc:
                conn.rollback()
                if transform_started:
                    failure_payload = {
                        "from_format": plan.from_format,
                        "to_format": plan.to_format,
                        "backup_path": str(backup_path),
                        "failed_at": datetime.now(UTC).isoformat(),
                        "error_type": type(exc).__name__,
                    }
                    try:
                        atomic_write_text(
                            contained_path(workspace, MIGRATION_FAILURE_MARKER),
                            json.dumps(failure_payload, indent=2) + "\n",
                        )
                    except OSError as marker_exc:
                        exc.add_note(
                            "Ferumind also failed to write the migration recovery marker "
                            f"({type(marker_exc).__name__})"
                        )
                raise

            return MigrationReport(
                plan=plan,
                dry_run=False,
                backup_path=str(backup_path),
                snapshot_id=snapshot_id,
                reindexed_documents=result.documents_indexed,
            )
