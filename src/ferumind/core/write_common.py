"""Guards and the result shape every write domain shares.

The write surface is split by domain — patches, documents, uploads,
lifecycle, projects — but a handful of things sit under all of them: the path
validator that enforces the project boundary, the size ceiling on mutated
Markdown, the post-write reindex, the folder and title validators, and the
:class:`WriteResult` every committed mutation returns.

They live here, once, so no domain module has to import another domain module
to reach them. Everything here is imported from ``core.write_limits`` or from
modules below the write layer, so this module stays a leaf of the write graph.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from ferumind.core.errors import (
    FileTooLargeError,
    UnknownFolderError,
    UnsupportedFileTypeError,
    ValidationError,
    WorkspaceMismatchError,
)
from ferumind.core.folders import CREATABLE_FOLDERS
from ferumind.core.indexer import index_file
from ferumind.core.operations import OP_FAILED, record_operation
from ferumind.core.paths import (
    PathSafetyError,
    contained_path,
    contained_project_root,
    is_under_root,
)
from ferumind.core.types import DbConnection
from ferumind.core.write_limits import (
    ALLOWED_EXTENSIONS,
    MAX_MUTATED_MARKDOWN_BYTES,
    MAX_TITLE_CHARS,
)

logger = logging.getLogger(__name__)


class WriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    snapshot_id: str | None = None
    restored_from_snapshot_id: str | None = None
    rollback_snapshot_id: str | None = None
    path: str
    folder: str | None = None
    document_sha256: str | None = None
    diff: str = ""
    index_error: str | None = None


def validate_path_safe(
    workspace_root: Path,
    project_key: str,
    user_path: str,
    *,
    extensions_denylist: frozenset[str] | None = None,
) -> Path:
    """Validate a project-relative path is safe and within the project.

    By default, only ``ALLOWED_EXTENSIONS`` (Markdown) may be targeted. Pass
    ``extensions_denylist`` to invert that check instead — block only the
    given extensions and allow everything else (used by uploads, which are
    not Markdown).
    """
    project_root = contained_project_root(workspace_root, project_key)
    try:
        resolved = contained_path(project_root, user_path)
    except PathSafetyError as exc:
        raise WorkspaceMismatchError(
            f"Path {user_path!r} is outside the asserted project boundary"
        ) from exc
    if not is_under_root(resolved, project_root):
        msg = f"Path {user_path!r} escapes the project root"
        raise WorkspaceMismatchError(msg)
    suffix = resolved.suffix.lower()
    if any(part.startswith(".") for part in Path(user_path).parts):
        raise WorkspaceMismatchError("Hidden paths and .ferumind internals cannot be targeted")
    if extensions_denylist is not None:
        if suffix in extensions_denylist:
            msg = f"Uploads of {suffix!r} files are not allowed"
            raise UnsupportedFileTypeError(msg, details={"extension": suffix})
    elif suffix not in ALLOWED_EXTENSIONS:
        msg = f"Unsupported file extension {suffix}: only {sorted(ALLOWED_EXTENSIONS)} allowed"
        raise ValidationError(msg)
    return resolved


def validate_markdown_size(content: str) -> None:
    size = len(content.encode("utf-8"))
    if size > MAX_MUTATED_MARKDOWN_BYTES:
        raise FileTooLargeError(
            f"Markdown mutation is {size} bytes; maximum is {MAX_MUTATED_MARKDOWN_BYTES}",
            details={
                "size_bytes": size,
                "max_bytes": MAX_MUTATED_MARKDOWN_BYTES,
                "scope": "document",
            },
        )


def canonical_folder_path(folder_path: str, *, allow_empty: bool) -> str:
    """Return one unambiguous relative POSIX folder path."""
    if folder_path != folder_path.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in folder_path
    ):
        raise ValidationError(
            "folder_path must not have surrounding whitespace or control characters"
        )
    cleaned = folder_path.strip()
    if not cleaned:
        if allow_empty:
            return ""
        raise UnknownFolderError(
            "folder_path must start with a role folder",
            details={"allowed_folders": list(CREATABLE_FOLDERS)},
        )
    if (
        cleaned.startswith("/")
        or cleaned.endswith("/")
        or "\\" in cleaned
        or "//" in cleaned
        or any(part in (".", "..") for part in PurePosixPath(cleaned).parts)
        or PurePosixPath(cleaned).as_posix() != cleaned
    ):
        raise ValidationError("folder_path must be a canonical relative POSIX path")
    return cleaned


def validate_title(title: str) -> None:
    """Reject empty or excessively large user-facing titles."""
    if not title.strip():
        raise ValidationError("title must not be empty")
    if len(title) > MAX_TITLE_CHARS:
        raise ValidationError(
            f"title exceeds the {MAX_TITLE_CHARS}-character limit",
            details={"max_chars": MAX_TITLE_CHARS},
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in title):
        raise ValidationError("title must not contain control characters")


def reindex_after_write(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    target_file: Path,
) -> str | None:
    """Re-index a file after a write, recording a structured failure.

    Returns ``None`` on success or an error message string when re-indexing
    failed. A failed re-index is recorded as a failed operation so the audit
    log reflects the inconsistent index state.
    """
    project_dir = contained_project_root(workspace_root, project_key)
    rel = target_file.relative_to(project_dir).as_posix()
    try:
        index_file(conn, workspace_root, project_key, target_file)
    except (OSError, ValueError, sqlite3.Error) as exc:
        safe_error = f"Index refresh failed ({type(exc).__name__})"
        try:
            record_operation(
                conn,
                project_key=project_key,
                operation_type="reindex",
                target_path=rel,
                request_json={"error_type": type(exc).__name__},
                state=OP_FAILED,
            )
        except sqlite3.Error as audit_error:
            conn.rollback()
            logger.error(
                "Failed to record a derived-index error (type=%s)",
                type(audit_error).__name__,
            )
        return safe_error
    return None
