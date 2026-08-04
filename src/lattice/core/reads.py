"""Core read services for project documents, trees, and snapshots."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final, Literal

from lattice.core.documents import ParsedDocument, parse_document_content
from lattice.core.errors import (
    DocumentNotFoundError,
    SnapshotNotFoundError,
    ValidationError,
)
from lattice.core.folders import ROLE_FOLDERS
from lattice.core.paths import (
    PathSafetyError,
    WorkspaceRoot,
    contained_path,
    contained_project_root,
)
from lattice.core.snapshots import (
    SnapshotMetadata,
    find_snapshot_dir,
    read_snapshot_metadata,
)
from lattice.core.types import DbConnection, JsonObject, JsonValue, StrictModel

_TREE_FOLDERS = frozenset({"spine", *ROLE_FOLDERS})
MAX_SNAPSHOT_TEXT_BYTES: Final = 5 * 1024 * 1024
MAX_SNAPSHOT_DIFF_BYTES: Final = 5 * 1024 * 1024
MAX_SNAPSHOT_STORED_FILE_BYTES: Final = 64 * 1024 * 1024
_SNAPSHOT_HASH_CHUNK_BYTES: Final = 64 * 1024


class ProjectDocumentRead(StrictModel):
    path: str
    content: str
    parsed: ParsedDocument


class ProjectTreeEntry(StrictModel):
    path: str
    title: str
    folder: str
    status: str
    edit_policy: str
    size: int


class ProjectSnapshotRead(StrictModel):
    snapshot_id: str
    metadata: SnapshotMetadata
    target_path: str | None
    before_content: str | None
    after_content: str | None
    before_content_omitted: bool
    after_content_omitted: bool
    diff: str
    diff_omitted: bool


def read_project_document(
    workspace: WorkspaceRoot,
    project_key: str,
    path: str,
) -> ProjectDocumentRead:
    """Read one ordinary project Markdown document, excluding internals."""
    project_root = contained_project_root(workspace, project_key)
    resolved = contained_path(project_root, path)
    if (
        any(part.startswith(".") for part in Path(path).parts)
        or not resolved.is_file()
        or resolved.suffix.lower() != ".md"
    ):
        raise DocumentNotFoundError(f"Document not found: {path}")
    rel = resolved.relative_to(project_root).as_posix()
    content = resolved.read_text(encoding="utf-8")
    parsed = parse_document_content(content, project_key=project_key, path=rel)
    return ProjectDocumentRead(path=rel, content=content, parsed=parsed)


def list_project_tree(
    conn: DbConnection,
    project_key: str,
    *,
    folder: str | None = None,
) -> list[ProjectTreeEntry]:
    """List indexed project documents with an optional validated role filter."""
    if folder is not None and folder not in _TREE_FOLDERS:
        allowed_folders: list[JsonValue] = []
        allowed_folders.extend(sorted(_TREE_FOLDERS))
        details: JsonObject = {"allowed_folders": allowed_folders}
        raise ValidationError(
            f"Unknown tree folder {folder!r}",
            details=details,
        )
    params: list[object] = [project_key]
    sql = (
        "SELECT path, title, folder, status, edit_policy, size_bytes "
        "FROM documents WHERE project_key = ?"
    )
    if folder is not None:
        sql += " AND folder = ?"
        params.append(folder)
    sql += " ORDER BY folder, path"
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [
        ProjectTreeEntry(
            path=row["path"],
            title=row["title"],
            folder=row["folder"],
            status=row["status"],
            edit_policy=row["edit_policy"],
            size=row["size_bytes"],
        )
        for row in rows
    ]


def read_project_snapshot(
    workspace: WorkspaceRoot,
    project_key: str,
    snapshot_id: str,
) -> ProjectSnapshotRead:
    """Read and validate one project-file snapshot."""
    project_dir = contained_project_root(workspace, project_key)
    snapshot_dir = find_snapshot_dir(project_dir, snapshot_id)
    if snapshot_dir is None:
        raise SnapshotNotFoundError(f"Snapshot {snapshot_id} not found")
    metadata = read_snapshot_metadata(snapshot_dir)
    if metadata is None or metadata.id != snapshot_id or metadata.project_key != project_key:
        raise SnapshotNotFoundError(
            f"Snapshot {snapshot_id} metadata is missing, invalid, or mismatched"
        )
    target_path = metadata.target_path
    before: str | None = None
    after: str | None = None
    before_omitted = False
    after_omitted = False
    if target_path:
        before, before_omitted = _read_verified_snapshot_side(
            snapshot_dir,
            snapshot_id,
            "before",
            target_path,
            expected_sha256=metadata.before_sha256,
            expected_size_bytes=metadata.before_size_bytes,
        )
        after, after_omitted = _read_verified_snapshot_side(
            snapshot_dir,
            snapshot_id,
            "after",
            target_path,
            expected_sha256=metadata.after_sha256,
            expected_size_bytes=metadata.after_size_bytes,
        )
    elif any(
        value is not None
        for value in (
            metadata.before_sha256,
            metadata.before_size_bytes,
            metadata.after_sha256,
            metadata.after_size_bytes,
        )
    ):
        raise _snapshot_integrity_error(snapshot_id, "metadata")
    diff, diff_omitted = _read_bounded_snapshot_diff(snapshot_dir, snapshot_id)
    return ProjectSnapshotRead(
        snapshot_id=snapshot_id,
        metadata=metadata,
        target_path=target_path,
        before_content=before,
        after_content=after,
        before_content_omitted=before_omitted,
        after_content_omitted=after_omitted,
        diff=diff,
        diff_omitted=diff_omitted,
    )


def _snapshot_integrity_error(snapshot_id: str, component: str) -> SnapshotNotFoundError:
    return SnapshotNotFoundError(
        f"Snapshot {snapshot_id} {component} is missing, invalid, or corrupted"
    )


def _read_verified_snapshot_side(
    snapshot_dir: Path,
    snapshot_id: str,
    side: Literal["before", "after"],
    target_path: str,
    *,
    expected_sha256: str | None,
    expected_size_bytes: int | None,
) -> tuple[str | None, bool]:
    """Verify one stored side, omitting valid binary or oversized content."""
    try:
        target = contained_path(contained_path(snapshot_dir, side), target_path)
    except PathSafetyError:
        raise _snapshot_integrity_error(snapshot_id, side) from None

    exists = target.is_file()
    if expected_sha256 is None and expected_size_bytes is None:
        if exists:
            raise _snapshot_integrity_error(snapshot_id, side)
        return None, False
    if expected_sha256 is None or expected_size_bytes is None:
        raise _snapshot_integrity_error(snapshot_id, side)
    if not exists:
        raise _snapshot_integrity_error(snapshot_id, side)

    if expected_size_bytes < 0 or expected_size_bytes > MAX_SNAPSHOT_STORED_FILE_BYTES:
        raise _snapshot_integrity_error(snapshot_id, side)

    try:
        if target.stat().st_size != expected_size_bytes:
            raise _snapshot_integrity_error(snapshot_id, side)
        digest = hashlib.sha256()
        response_bytes = bytearray() if expected_size_bytes <= MAX_SNAPSHOT_TEXT_BYTES else None
        total_bytes = 0
        with target.open("rb") as stream:
            while chunk := stream.read(_SNAPSHOT_HASH_CHUNK_BYTES):
                total_bytes += len(chunk)
                if (
                    total_bytes > expected_size_bytes
                    or total_bytes > MAX_SNAPSHOT_STORED_FILE_BYTES
                ):
                    raise _snapshot_integrity_error(snapshot_id, side)
                digest.update(chunk)
                if response_bytes is not None:
                    response_bytes.extend(chunk)
    except OSError:
        raise _snapshot_integrity_error(snapshot_id, side) from None

    if total_bytes != expected_size_bytes or digest.hexdigest() != expected_sha256:
        raise _snapshot_integrity_error(snapshot_id, side)
    if response_bytes is None:
        return None, True
    try:
        return bytes(response_bytes).decode("utf-8"), False
    except UnicodeDecodeError:
        return None, True


def _read_bounded_snapshot_diff(
    snapshot_dir: Path,
    snapshot_id: str,
) -> tuple[str, bool]:
    """Read a diff with a hard byte cap and explicit omission state."""
    try:
        diff_file = contained_path(snapshot_dir, "diff.patch")
        if not diff_file.is_file():
            return "", False
        with diff_file.open("rb") as stream:
            raw = stream.read(MAX_SNAPSHOT_DIFF_BYTES + 1)
    except (OSError, PathSafetyError):
        raise _snapshot_integrity_error(snapshot_id, "diff") from None
    if len(raw) > MAX_SNAPSHOT_DIFF_BYTES:
        return "", True
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return "", True
