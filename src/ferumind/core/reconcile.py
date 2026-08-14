"""Reconcile-on-read: out-of-band disk edits are first-class (00 D12).

Every core read that serves content or maps calls into this module. The
check is a cheap stat (mtime_ns + size) against the index; only on drift
does it rehash and reindex the file, mark pending proposals bound to the old
document hash as stale, and write an operation-log entry with
``source: out-of-band``.

This is the *only* mechanism that detects out-of-band edits. There is no
filesystem watcher: the liveness layer was removed in favour of reconcile-on-read,
which covers the same edits at the moment they matter (the next read) without a
supervised background process. See ``product/00-what-is-ferumind.md``.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from ferumind.core.errors import WorkspaceMismatchError
from ferumind.core.indexer import (
    get_indexed_signature,
    index_file,
    project_dir_for,
    remove_from_index,
    stat_signature,
)
from ferumind.core.operations import (
    SOURCE_OUT_OF_BAND,
    mark_stale_proposals,
    record_operation,
)
from ferumind.core.paths import PathSafetyError, contained_path
from ferumind.core.types import DbConnection


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
        # Hidden files and .ferumind internals are never indexed; nothing to
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
