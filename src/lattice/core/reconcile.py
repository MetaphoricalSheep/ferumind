"""Reconcile-on-read: out-of-band disk edits are first-class (00 D12).

Every core read that serves content or maps calls into this module. The
check is a cheap stat (mtime_ns + size) against the index; only on drift
does it rehash and reindex the file, mark pending proposals bound to the old
document hash as stale, and write an operation-log entry with
``source: out-of-band``. The watcher (liveness layer) uses
:func:`record_watch_detection` to add a snapshot on detect.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from lattice.core.documents import compute_sha256
from lattice.core.errors import WorkspaceMismatchError
from lattice.core.indexer import (
    get_indexed_signature,
    index_file,
    project_dir_for,
    remove_from_index,
    stat_signature,
)
from lattice.core.operations import (
    SOURCE_OUT_OF_BAND,
    SOURCE_WATCHER,
    mark_stale_proposals,
    record_operation,
)
from lattice.core.paths import PathSafetyError, contained_path
from lattice.core.snapshots import create_snapshot, new_snapshot_id, record_snapshot_in_db
from lattice.core.types import DbConnection


class ReconcileOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    drifted: bool = False
    removed: bool = False
    proposals_staled: int = 0


def reconcile_document(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    path: str,
    *,
    source: str = SOURCE_OUT_OF_BAND,
) -> ReconcileOutcome:
    """Reconcile one document path against the index; cheap when unchanged.

    The path is containment-checked before any filesystem access: reconcile
    runs ahead of the read-path validators, so it must never stat or index
    anything outside the project root.
    """
    project_dir = project_dir_for(workspace_root, project_key)
    try:
        file_path = contained_path(project_dir, path)
    except PathSafetyError as exc:
        raise WorkspaceMismatchError(
            f"Path {path!r} is outside the asserted project boundary"
        ) from exc
    parts = PurePosixPath(path).parts
    if not path.endswith(".md") or any(part.startswith(".") for part in parts):
        # Hidden files and .lattice internals are never indexed; nothing to
        # reconcile.
        return ReconcileOutcome()
    on_disk = stat_signature(file_path)
    indexed = get_indexed_signature(conn, project_key, path)

    if on_disk is None:
        if indexed is None:
            return ReconcileOutcome()
        # File vanished out-of-band: drop the row, stale everything pending.
        remove_from_index(conn, project_key, path)
        staled = mark_stale_proposals(
            conn, project_key=project_key, target_path=path, current_sha256=None
        )
        record_operation(
            conn,
            project_key=project_key,
            operation_type="out_of_band_delete",
            target_path=path,
            source=source,
            base_sha256=indexed[2],
        )
        return ReconcileOutcome(drifted=True, removed=True, proposals_staled=staled)

    if indexed is not None and (on_disk[0], on_disk[1]) == (indexed[0], indexed[1]):
        return ReconcileOutcome()

    old_sha256 = indexed[2] if indexed is not None else None
    parsed = index_file(conn, workspace_root, project_key, file_path)
    if old_sha256 is not None and parsed.sha256 == old_sha256:
        # Touched but content-identical (e.g. editor re-save): index row was
        # refreshed with the new stat; nothing else to do.
        return ReconcileOutcome()

    staled = mark_stale_proposals(
        conn, project_key=project_key, target_path=path, current_sha256=parsed.sha256
    )
    record_operation(
        conn,
        project_key=project_key,
        operation_type="out_of_band_edit",
        target_path=path,
        source=source,
        base_sha256=old_sha256,
        after_sha256=parsed.sha256,
    )
    return ReconcileOutcome(drifted=True, proposals_staled=staled)


def reconcile_project(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
) -> int:
    """Reconcile every document in a project; returns the drift count.

    Used by project-wide reads (``get_context``, ``search_project``,
    ``list_tree``). Stat-only per file unless drift is found.
    """
    project_dir = project_dir_for(workspace_root, project_key)
    drifted = 0
    on_disk_paths: set[str] = set()
    if project_dir.is_dir():
        for md_file in sorted(project_dir.rglob("*.md")):
            rel = md_file.relative_to(project_dir)
            if any(part.startswith(".") for part in rel.parts):
                continue
            rel_str = rel.as_posix()
            on_disk_paths.add(rel_str)
            outcome = reconcile_document(conn, workspace_root, project_key, rel_str)
            if outcome.drifted:
                drifted += 1
    rows = conn.execute(
        "SELECT path FROM documents WHERE project_key = ?", (project_key,)
    ).fetchall()
    for row in rows:
        if row["path"] not in on_disk_paths:
            outcome = reconcile_document(conn, workspace_root, project_key, str(row["path"]))
            if outcome.drifted:
                drifted += 1
    return drifted


def record_watch_detection(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    path: str,
) -> ReconcileOutcome:
    """Watcher path: snapshot-on-detect, then reconcile (reindex + oplog).

    The snapshot captures the on-disk content at detection time so the
    hand-edit is recoverable even before any agent read happens. Snapshot
    publication deliberately precedes reconcile: if snapshot creation fails,
    the indexed signature remains stale and a later watcher pass can retry
    instead of permanently losing the recovery point.
    """
    project_dir = project_dir_for(workspace_root, project_key)
    try:
        file_path = contained_path(project_dir, path)
    except PathSafetyError as exc:
        raise WorkspaceMismatchError(
            f"Path {path!r} is outside the asserted project boundary"
        ) from exc
    on_disk = stat_signature(file_path)
    indexed = get_indexed_signature(conn, project_key, path)
    if on_disk is not None and (
        indexed is None or (on_disk[0], on_disk[1]) != (indexed[0], indexed[1])
    ):
        content = file_path.read_text(encoding="utf-8")
        if indexed is None or compute_sha256(content) != indexed[2]:
            snapshot_id = new_snapshot_id()
            snapshot_dir = create_snapshot(
                project_dir,
                project_key=project_key,
                target_path=path,
                before_content=content,
                after_content=None,
                reason="watch_detect",
                snapshot_id=snapshot_id,
            )
            record_snapshot_in_db(
                conn,
                snapshot_id=snapshot_id,
                project_key=project_key,
                target_path=path,
                snapshot_dir=str(snapshot_dir),
                reason="watch_detect",
            )
    return reconcile_document(
        conn,
        workspace_root,
        project_key,
        path,
        source=SOURCE_WATCHER,
    )
