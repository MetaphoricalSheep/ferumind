"""Workspace format migration frame (product/spec-versioning.md §1.3).

Migration of user Markdown is never implicit: ``ferumind migrate`` is the
only entry point. Ferumind ships the frame with an **empty** migrator
registry, because format 1 is the floor and nothing precedes it. The registry
fills when the first format bump needs it, and a bump never lands without its
migrator, fixtures, and tests in the same change.

Flow: resolve the ``N → N+1`` migrator chain, create a full workspace
tarball backup and a global snapshot, run each migrator, rebuild the index,
commit one operation-log entry per project, then publish ``meta.yml`` as the
final step that re-enables writes.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tarfile
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ferumind.core.errors import FormatUnsupportedError, MigrationPrerequisiteError
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

logger = logging.getLogger(__name__)

#: A migrator transforms the workspace tree in place from format N to N+1.
type Migrator = Callable[[WorkspaceRoot], None]

#: A preflight validates a format's semantic prerequisites and returns the
#: reasons the workspace is not ready — empty meaning ready. It must not
#: write, and :func:`run_migration` invokes it under the workspace and every
#: registered-project lock before the backup exists.
type Preflight = Callable[[WorkspaceRoot], list[str]]

#: Registered migrators keyed by their source format (N → N+1). Deliberately
#: empty between format bumps; tests register fake steps against the frame.
MIGRATORS: dict[int, Migrator] = {}

#: Prerequisite validators keyed by the same source format. A step may have a
#: migrator and no preflight; the reverse is meaningless and never consulted.
PREFLIGHTS: dict[int, Preflight] = {}

MIGRATION_FAILURE_MARKER = ".ferumind/MIGRATION_RECOVERY_REQUIRED.json"
_MAX_RECOVERY_MARKER_BYTES = 64 * 1024


class MigrationExecutionError(RuntimeError):
    """A migration failed after backup and transformation semantics began."""


class MigrationRecoveryState(BaseModel):
    """Durable replay guard spanning transformation through marker publication."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["transforming", "audit_committed", "failed"]
    from_format: int
    to_format: int
    backup_path: str
    snapshot_id: str
    started_at: str
    failed_at: str | None = None
    error_type: str | None = None


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
    found = read_format(workspace)
    if recovery_marker.is_file():
        recovery_state = _read_migration_recovery_state(recovery_marker)
        completed_publication = (
            recovery_state is not None
            and recovery_state.state == "audit_committed"
            and recovery_state.to_format == found
        )
        if not completed_publication:
            raise FormatUnsupportedError(
                "A previous workspace migration may have made changes; "
                "restore its recorded backup before retrying",
                details={"recovery_marker": MIGRATION_FAILURE_MARKER},
            )
        logger.warning(
            "Ignoring a stale migration recovery marker after format %s was durably published",
            found,
        )
    registry = MIGRATORS if migrators is None else migrators
    if found is None:
        raise FormatUnsupportedError(
            "No readable workspace format marker at system/meta.yml; there is "
            "no starting format to migrate from. Initialize the workspace with "
            "scripts/bootstrap_workspace.py, or point FERUMIND_WORKSPACE at an "
            "existing workspace.",
            details={"marker": "system/meta.yml", "target_format": target_format},
        )
    current = found
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


def run_preflights(
    workspace: WorkspaceRoot,
    steps: list[int],
    *,
    preflights: dict[int, Preflight] | None = None,
) -> None:
    """Validate every step's semantic prerequisites, or refuse having done nothing.

    This runs **before** :func:`create_backup_tarball`, before the durable
    replay guard is armed, and before ``transform_started`` is set; that
    placement is the whole point. Once transformation can begin, the guard
    blocks every future migration until either backup recovery or proven final
    publication. That is right for a tree that may be half-converted and badly
    wrong for a workspace that simply is not ready yet: three missing
    descriptions should be reported and cost nothing, not put the workspace
    into recovery-required.

    So a prerequisite failure here raises before any of that exists — no
    backup, no snapshot, no marker, nothing on disk changed.
    """
    registry = PREFLIGHTS if preflights is None else preflights
    failures: list[str] = []
    for step in steps:
        preflight = registry.get(step)
        if preflight is None:
            continue
        failures.extend(preflight(workspace))
    if failures:
        shown = failures[:20]
        detail = "; ".join(shown)
        if len(failures) > len(shown):
            detail += f"; and {len(failures) - len(shown)} more"
        raise MigrationPrerequisiteError(
            f"Workspace is not ready to migrate: {detail}",
            details={"failure_count": len(failures)},
        )


def _project_roots_for_migration(workspace: WorkspaceRoot) -> dict[str, Path]:
    """Resolve every registered project before trying to acquire any lock.

    Project locks live inside project directories, so a missing or symlinked
    registered directory cannot be locked. Treat that as a prerequisite
    failure under the workspace lock, before backup or transformation, rather
    than silently omitting the project from preflight and rebuild.
    """
    roots: dict[str, Path] = {}
    failures: list[str] = []
    for key in sorted(load_registry(workspace)):
        try:
            root = contained_project_root(workspace, key)
        except PathSafetyError as exc:
            failures.append(f"registered project {key!r} has an unsafe path ({exc})")
            continue
        if not root.is_dir():
            failures.append(f"registered project {key!r} has no project directory")
            continue
        roots[key] = root
    if failures:
        raise MigrationPrerequisiteError(
            f"Workspace is not ready to migrate: {'; '.join(failures)}",
            details={"failure_count": len(failures)},
        )
    return roots


def _raise_on_rebuild_errors(errors: int, messages: list[str]) -> None:
    if errors == 0:
        return
    shown = messages[:20]
    detail = "; ".join(shown)
    if len(messages) > len(shown):
        detail += f"; and {len(messages) - len(shown)} more"
    raise MigrationExecutionError(
        f"Migration index rebuild failed for {errors} document(s): {detail}"
    )


def _read_migration_recovery_state(marker: Path) -> MigrationRecoveryState | None:
    """Read a bounded recovery marker; malformed state always fails closed."""
    try:
        if marker.stat().st_size > _MAX_RECOVERY_MARKER_BYTES:
            return None
        return MigrationRecoveryState.model_validate_json(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, ValueError):
        return None


def _write_migration_recovery_state(
    workspace: WorkspaceRoot,
    state: MigrationRecoveryState,
) -> None:
    """Durably publish replay state or fail before transformation continues.

    A readable replacement after a directory-fsync error is not enough: a
    power loss may still discard that directory entry. Callers therefore
    propagate every durability error. For the initial ``transforming`` state,
    that guarantees the migrator never runs unless its replay guard is durable.
    """
    marker = contained_path(workspace, MIGRATION_FAILURE_MARKER)
    atomic_write_text(marker, state.model_dump_json(indent=2) + "\n")


def _fsync_directory(directory: Path) -> None:
    """Make prior directory-entry changes durable."""
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _clear_completed_recovery_state(workspace: WorkspaceRoot) -> None:
    """Remove the replay guard after format publication, with safe ambiguity.

    Failure to remove the file cannot make a completed migration unsafe. Its
    ``audit_committed`` state plus the target format tells
    :func:`plan_migration` that publication completed, so a lingering marker
    is ignored rather than replayed.
    """
    marker = contained_path(workspace, MIGRATION_FAILURE_MARKER)
    try:
        marker.unlink(missing_ok=True)
        _fsync_directory(marker.parent)
    except OSError as exc:
        logger.warning(
            "Could not durably clear completed migration recovery state "
            "(type=%s); its committed phase remains safe",
            type(exc).__name__,
        )


def _publish_format_marker(workspace: WorkspaceRoot, target_format: int) -> bool:
    """Publish the final marker, resolving a post-replace fsync ambiguity.

    ``atomic_write_text`` can replace the marker successfully and then report
    a directory-fsync failure. At this point migration audit rows are already
    committed. If the intended value is visible, retry the directory fsync.
    Return ``False`` if durability remains uncertain so the caller retains the
    durable ``audit_committed`` replay guard; a later power loss can then
    restore the old format marker without making the migration replayable.
    """
    try:
        write_format_marker(workspace, target_format)
    except OSError:
        if read_format(workspace) != target_format:
            raise
        try:
            _fsync_directory(contained_path(workspace, "system"))
        except OSError:
            logger.warning(
                "Format %s is visible but its directory entry could not be "
                "proven durable; retaining the audit-committed replay guard",
                target_format,
            )
            return False
        logger.warning(
            "Format marker publication reported failure after format %s became "
            "visible; an explicit directory fsync proved it durable",
            target_format,
        )
    return True


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
    # Backups are whole-workspace archives, so this directory is as sensitive
    # as the workspace itself and its mode is re-asserted on every migration
    # rather than only set on create. See ``core.file_io`` for the rule.
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
    preflights: dict[int, Preflight] | None = None,
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
        project_roots = _project_roots_for_migration(workspace)
        with ExitStack() as project_locks:
            for key, project_root in project_roots.items():
                project_locks.enter_context(acquire_project_lock(project_root, key))

            if sorted(load_registry(workspace)) != sorted(project_roots):
                raise MigrationPrerequisiteError(
                    "Workspace is not ready to migrate: the project registry changed "
                    "while migration locks were being acquired"
                )

            # The full prerequisite read now has the same exclusion boundary
            # as the backup and transformation that follow it: one workspace
            # lock plus every registered-project lock.
            run_preflights(workspace, plan.steps, preflights=preflights)

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

            recovery_state = MigrationRecoveryState(
                state="transforming",
                from_format=plan.from_format,
                to_format=plan.to_format,
                backup_path=str(backup_path),
                snapshot_id=snapshot_id,
                started_at=datetime.now(UTC).isoformat(),
            )
            # Arm the replay guard durably before the first transformation.
            # A process death from this point onward must require backup
            # recovery instead of replaying an in-place migrator.
            _write_migration_recovery_state(workspace, recovery_state)

            transform_started = False
            try:
                transform_started = True
                for step in plan.steps:
                    registry[step](workspace)

                project_keys = sorted(load_registry(workspace))
                for key in project_keys:
                    if key not in project_roots:
                        project_root = contained_project_root(workspace, key)
                        project_locks.enter_context(acquire_project_lock(project_root, key))
                        project_roots[key] = project_root
                result = rebuild_index(conn, workspace, project_keys, locks_held=True)
                _raise_on_rebuild_errors(result.errors, result.error_messages)

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

                # Distinguish a power loss before marker publication from one
                # after it. Only this phase plus the target marker proves the
                # migration completed and makes a lingering state file stale.
                recovery_state = recovery_state.model_copy(update={"state": "audit_committed"})
                _write_migration_recovery_state(workspace, recovery_state)

                # The marker is the declaration that the cutover is complete
                # and re-enables writes. Make it the final durable step: an
                # audit-row or commit failure must leave the old marker in
                # place even though transformation semantics have begun.
                publication_durable = _publish_format_marker(workspace, plan.to_format)
                if publication_durable:
                    _clear_completed_recovery_state(workspace)
            except BaseException as exc:
                conn.rollback()
                if transform_started:
                    failed_state = recovery_state.model_copy(
                        update={
                            "state": "failed",
                            "failed_at": datetime.now(UTC).isoformat(),
                            "error_type": type(exc).__name__,
                        }
                    )
                    try:
                        _write_migration_recovery_state(workspace, failed_state)
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
