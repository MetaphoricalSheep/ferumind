"""Archive lifecycle and snapshot restore.

All project-scoped writes go through the write modules, never through direct
filesystem access in MCP/CLI layers. Every mutation here is snapshot-protected
and operation-logged.

The other write domains each have their own module: the guarded
propose → apply transaction in :mod:`ferumind.core.patch_writes`; creating
documents, capturing notes, and recording episodes in
:mod:`ferumind.core.document_writes`; every way bytes reach ``library/`` in
:mod:`ferumind.core.upload_writes`; and project creation in
:mod:`ferumind.core.project_writes`. The bounds every write is measured
against live in :mod:`ferumind.core.write_limits`; the path/size/title guards
and :class:`~ferumind.core.write_common.WriteResult` they all share live in
:mod:`ferumind.core.write_common`.

Hard refusals (the closed list, 00 principle 1): archived targets
(``DOCUMENT_ARCHIVED``), protected frontmatter identity keys
(``FRONTMATTER_PROTECTED``), out-of-project paths (``WORKSPACE_MISMATCH``).
Everything else — edit policies, frozen structure — is echoed to the agent,
not blocked.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ferumind.core.documents import compute_sha256, parse_document_content
from ferumind.core.errors import (
    CannotArchiveSpineError,
    DocumentArchivedError,
    DocumentNotFoundError,
    InvalidOperationError,
    PathExistsError,
    SnapshotNotFoundError,
)
from ferumind.core.file_io import atomic_write_text
from ferumind.core.folders import (
    SPINE_FILENAME,
    archive_path_for,
    folder_of,
    is_archived_path,
    origin_path_for,
)
from ferumind.core.frontmatter import (
    extract_frontmatter_block,
    set_frontmatter_updated,
)
from ferumind.core.indexer import remove_from_index
from ferumind.core.locks import acquire_project_lock
from ferumind.core.operations import (
    OP_APPLIED,
    OP_FAILED,
    record_operation,
)
from ferumind.core.paths import contained_project_root
from ferumind.core.reconcile import reconcile_document
from ferumind.core.snapshots import (
    create_snapshot,
    find_snapshot_dir,
    new_snapshot_id,
    read_snapshot_before_content,
    read_snapshot_metadata,
    record_snapshot_in_db,
)
from ferumind.core.types import DbConnection
from ferumind.core.write_common import (
    WriteResult,
    reindex_after_write,
    validate_markdown_size,
    validate_path_safe,
)
from ferumind.core.write_limits import MAX_MUTATED_MARKDOWN_BYTES

logger = logging.getLogger(__name__)


def _remove_from_index_after_write(
    conn: DbConnection,
    project_key: str,
    path: str,
) -> str | None:
    """Remove a stale derived-index row without falsifying mutation failure."""
    try:
        remove_from_index(conn, project_key, path)
    except (OSError, ValueError, sqlite3.Error) as exc:
        conn.rollback()
        safe_error = f"Index removal failed ({type(exc).__name__})"
        try:
            record_operation(
                conn,
                project_key=project_key,
                operation_type="reindex",
                target_path=path,
                request_json={"error_type": type(exc).__name__},
                state=OP_FAILED,
            )
        except sqlite3.Error as audit_error:
            conn.rollback()
            logger.error(
                "Failed to record a derived-index removal error (type=%s)",
                type(audit_error).__name__,
            )
        return safe_error
    return None


def _combine_index_errors(*errors: str | None) -> str | None:
    present = [error for error in errors if error is not None]
    return "; ".join(present) if present else None


# ── Archive lifecycle ────────────────────────────────────────────────────────


class ArchiveResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    snapshot_id: str
    path: str
    archived_path: str
    document_sha256: str
    index_error: str | None = None


def archive_document(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    path: str,
) -> ArchiveResult:
    """Archive a document: ``status: archived`` plus a move to ``archive/<path>``."""
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        target_file = validate_path_safe(workspace_root, project_key, path)
        reconcile_document(conn, workspace_root, project_key, path)
        if not target_file.is_file():
            raise DocumentNotFoundError(f"Document not found: {path}")
        if path == SPINE_FILENAME:
            raise CannotArchiveSpineError("The spine cannot be archived")
        if is_archived_path(path):
            raise DocumentArchivedError(f"{path} is already under archive/")
        content = target_file.read_text(encoding="utf-8")
        parsed = parse_document_content(content, project_key=project_key, path=path)
        if parsed.status == "archived":
            raise DocumentArchivedError(f"{path} is already archived")

        archived_rel = archive_path_for(path)
        archived_file = validate_path_safe(workspace_root, project_key, archived_rel)
        if archived_file.exists():
            raise PathExistsError(
                f"Archive target already exists: {archived_rel}",
                details={"archived_path": archived_rel},
            )

        new_content = _set_status(content, "archived")
        new_sha256 = compute_sha256(new_content)

        snapshot_id = new_snapshot_id()
        snapshot_dir = create_snapshot(
            project_dir,
            project_key=project_key,
            target_path=path,
            before_content=content,
            after_content=None,
            reason="archive_document",
            snapshot_id=snapshot_id,
        )
        try:
            atomic_write_text(archived_file, new_content)
            target_file.unlink()
            record_snapshot_in_db(
                conn,
                snapshot_id=snapshot_id,
                project_key=project_key,
                target_path=path,
                snapshot_dir=str(snapshot_dir),
                reason="archive_document",
                commit=False,
            )
            op_id = record_operation(
                conn,
                project_key=project_key,
                operation_type="archive_document",
                tool_name="archive_document",
                target_path=path,
                request_json={"archived_path": archived_rel},
                base_sha256=parsed.sha256,
                after_sha256=new_sha256,
                snapshot_id=snapshot_id,
                state=OP_APPLIED,
                commit=False,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            atomic_write_text(target_file, content)
            archived_file.unlink(missing_ok=True)
            try:
                shutil.rmtree(snapshot_dir)
            except OSError as cleanup_error:
                logger.error(
                    "Archive rollback could not remove its snapshot (type=%s)",
                    type(cleanup_error).__name__,
                )
            raise

        removal_error = _remove_from_index_after_write(conn, project_key, path)
        reindex_error = reindex_after_write(
            conn,
            workspace_root,
            project_key,
            archived_file,
        )
        index_error = _combine_index_errors(removal_error, reindex_error)

    return ArchiveResult(
        operation_id=op_id,
        snapshot_id=snapshot_id,
        path=path,
        archived_path=archived_rel,
        document_sha256=new_sha256,
        index_error=index_error,
    )


def unarchive_document(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    archived_path: str,
) -> ArchiveResult:
    """Reverse an archive: move back to the mirror origin with ``status: active``."""
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        archived_file = validate_path_safe(workspace_root, project_key, archived_path)
        reconcile_document(conn, workspace_root, project_key, archived_path)
        if not archived_file.is_file():
            raise DocumentNotFoundError(f"Document not found: {archived_path}")
        origin_rel = origin_path_for(archived_path)
        origin_file = validate_path_safe(workspace_root, project_key, origin_rel)
        if origin_file.exists():
            raise PathExistsError(
                f"Cannot unarchive: {origin_rel} already exists",
                details={"path": origin_rel},
            )

        content = archived_file.read_text(encoding="utf-8")
        old_sha256 = compute_sha256(content)
        new_content = _set_status(content, "active")
        new_sha256 = compute_sha256(new_content)

        snapshot_id = new_snapshot_id()
        snapshot_dir = create_snapshot(
            project_dir,
            project_key=project_key,
            target_path=archived_path,
            before_content=content,
            after_content=None,
            reason="unarchive_document",
            snapshot_id=snapshot_id,
        )
        try:
            atomic_write_text(origin_file, new_content)
            archived_file.unlink()
            record_snapshot_in_db(
                conn,
                snapshot_id=snapshot_id,
                project_key=project_key,
                target_path=archived_path,
                snapshot_dir=str(snapshot_dir),
                reason="unarchive_document",
                commit=False,
            )
            op_id = record_operation(
                conn,
                project_key=project_key,
                operation_type="unarchive_document",
                tool_name="unarchive_document",
                target_path=origin_rel,
                request_json={"archived_path": archived_path},
                base_sha256=old_sha256,
                after_sha256=new_sha256,
                snapshot_id=snapshot_id,
                state=OP_APPLIED,
                commit=False,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            atomic_write_text(archived_file, content)
            origin_file.unlink(missing_ok=True)
            try:
                shutil.rmtree(snapshot_dir)
            except OSError as cleanup_error:
                logger.error(
                    "Unarchive rollback could not remove its snapshot (type=%s)",
                    type(cleanup_error).__name__,
                )
            raise

        removal_error = _remove_from_index_after_write(conn, project_key, archived_path)
        reindex_error = reindex_after_write(
            conn,
            workspace_root,
            project_key,
            origin_file,
        )
        index_error = _combine_index_errors(removal_error, reindex_error)

    return ArchiveResult(
        operation_id=op_id,
        snapshot_id=snapshot_id,
        path=origin_rel,
        archived_path=archived_path,
        document_sha256=new_sha256,
        index_error=index_error,
    )


def _set_status(content: str, status: str) -> str:
    """Set ``status`` in the frontmatter block, adding the key if missing."""
    fm_block, body = extract_frontmatter_block(content)
    if not fm_block:
        # Unmanaged file: archive still works, status lives only in the path.
        return content
    status_pattern = re.compile(r"^status\s*:.*$", re.MULTILINE)
    if status_pattern.search(fm_block):
        new_block = status_pattern.sub(f"status: {status}", fm_block, count=1)
    else:
        lines = fm_block.split("\n")
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == "---":
                lines.insert(i, f"status: {status}")
                break
        new_block = "\n".join(lines)
    now = datetime.now(UTC).isoformat()
    return set_frontmatter_updated(new_block, now) + body


# ── Restore ──────────────────────────────────────────────────────────────────


def restore_snapshot(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    snapshot_id: str,
) -> WriteResult:
    """Restore a file from a snapshot.

    Creates a pre-restore snapshot first, then restores the target snapshot.
    Both operations are snapshot-protected and logged.
    """
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        snapshot_dir = find_snapshot_dir(project_dir, snapshot_id)
        if snapshot_dir is None:
            raise SnapshotNotFoundError(
                f"Snapshot {snapshot_id} not found for project {project_key}"
            )

        metadata = read_snapshot_metadata(snapshot_dir)
        if metadata is None or metadata.id != snapshot_id or metadata.project_key != project_key:
            raise SnapshotNotFoundError(
                f"Snapshot {snapshot_id} metadata is missing, invalid, or mismatched"
            )
        target_path_str = metadata.target_path
        if not target_path_str:
            raise SnapshotNotFoundError(f"Snapshot {snapshot_id} has no target_path")
        if metadata.operation_type in {"archive_document", "unarchive_document"}:
            raise InvalidOperationError(
                "Archive transition snapshots cannot be restored as a single file; "
                "use archive_document or unarchive_document instead",
                details={"snapshot_reason": metadata.operation_type},
            )
        if metadata.before_sha256 is None or metadata.before_size_bytes is None:
            raise SnapshotNotFoundError(f"Snapshot {snapshot_id} has no verifiable before-content")

        target_file = validate_path_safe(workspace_root, project_key, target_path_str)
        restored_content = read_snapshot_before_content(
            snapshot_dir,
            target_path_str,
            expected_sha256=metadata.before_sha256,
            expected_size_bytes=metadata.before_size_bytes,
            max_bytes=MAX_MUTATED_MARKDOWN_BYTES,
        )
        if restored_content is None:
            msg = f"Snapshot {snapshot_id} before-content is missing or failed integrity checks"
            raise SnapshotNotFoundError(msg)
        validate_markdown_size(restored_content)
        parse_document_content(
            restored_content,
            project_key=project_key,
            path=target_path_str,
        )

        current_content = ""
        target_existed = target_file.is_file()
        if target_existed:
            current_content = target_file.read_text(encoding="utf-8")

        before_snapshot_id = new_snapshot_id()
        before_snapshot_dir = create_snapshot(
            project_dir,
            project_key=project_key,
            target_path=target_path_str,
            before_content=current_content if target_existed else None,
            after_content=None,
            reason="pre_restore_snapshot",
            snapshot_id=before_snapshot_id,
        )
        after_sha256 = compute_sha256(restored_content)
        try:
            atomic_write_text(target_file, restored_content)
            record_snapshot_in_db(
                conn,
                snapshot_id=before_snapshot_id,
                project_key=project_key,
                target_path=target_path_str,
                snapshot_dir=str(before_snapshot_dir),
                reason="pre_restore_snapshot",
                commit=False,
            )
            op_id = record_operation(
                conn,
                project_key=project_key,
                operation_type="restore_snapshot",
                tool_name="restore_snapshot",
                target_path=target_path_str,
                request_json={
                    "restored_from_snapshot_id": snapshot_id,
                    "rollback_snapshot_id": before_snapshot_id,
                },
                base_sha256=compute_sha256(current_content) if target_existed else None,
                after_sha256=after_sha256,
                snapshot_id=before_snapshot_id,
                state=OP_APPLIED,
                commit=False,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            if target_existed:
                atomic_write_text(target_file, current_content)
            else:
                target_file.unlink(missing_ok=True)
            try:
                shutil.rmtree(before_snapshot_dir)
            except OSError as cleanup_error:
                logger.error(
                    "Restore rollback could not remove its snapshot (type=%s)",
                    type(cleanup_error).__name__,
                )
            raise

        index_error = reindex_after_write(conn, workspace_root, project_key, target_file)

    return WriteResult(
        operation_id=op_id,
        snapshot_id=before_snapshot_id,
        restored_from_snapshot_id=snapshot_id,
        rollback_snapshot_id=before_snapshot_id,
        path=target_path_str,
        folder=folder_of(target_path_str),
        document_sha256=after_sha256,
        index_error=index_error,
    )
