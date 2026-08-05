"""Content indexing: Markdown files → the ``documents`` table + FTS5 mirror.

The index is derived state, rebuildable from Markdown at any time. Rows are
keyed by ``(project_key, path)`` so re-indexing replaces instead of
accumulating. Each row stores ``mtime_ns``/``size_bytes`` so reads can
detect out-of-band drift with a stat call (reconcile-on-read, 00 D12).
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ferumind.core.documents import ParsedDocument, parse_document
from ferumind.core.locks import acquire_project_lock
from ferumind.core.paths import contained_path, contained_project_root
from ferumind.core.types import DbConnection


class IndexResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents_indexed: int = 0
    documents_removed: int = 0
    errors: int = 0
    error_messages: list[str] = Field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _should_skip(rel_path: Path) -> bool:
    """Skip hidden files and ``.ferumind`` internals during indexing."""
    return any(part.startswith(".") for part in rel_path.parts)


def project_dir_for(workspace_root: Path, project_key: str) -> Path:
    """Return a validated, symlink-free project root."""
    return contained_project_root(workspace_root, project_key)


def stat_signature(file_path: Path) -> tuple[int, int] | None:
    """Return ``(mtime_ns, size_bytes)`` or ``None`` if the file is gone."""
    try:
        stat = file_path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def index_file(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    file_path: Path,
) -> ParsedDocument:
    """Parse and index a single Markdown file; returns the parsed document."""
    project_dir = project_dir_for(workspace_root, project_key)
    parsed = parse_document(file_path, project_key=project_key, project_root=project_dir)
    signature = stat_signature(file_path) or (0, 0)
    _upsert_document(conn, parsed, mtime_ns=signature[0], size_bytes=signature[1])
    conn.commit()
    return parsed


def remove_from_index(conn: DbConnection, project_key: str, path: str) -> None:
    """Remove a document (e.g. moved or deleted on disk) from the index."""
    conn.execute(
        "DELETE FROM documents WHERE project_key = ? AND path = ?",
        (project_key, path),
    )
    conn.execute(
        "DELETE FROM search_index WHERE project_key = ? AND path = ?",
        (project_key, path),
    )
    conn.commit()


def index_project(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
) -> IndexResult:
    """Walk a project directory and index all Markdown files.

    Also removes index rows whose files no longer exist on disk, so the
    index converges to the filesystem truth.
    """
    result = IndexResult()
    project_dir = project_dir_for(workspace_root, project_key)
    if not project_dir.is_dir():
        result.error_messages.append(f"Project directory not found for {project_key!r}")
        result.errors += 1
        return result

    seen_paths: set[str] = set()
    for md_file in sorted(project_dir.rglob("*.md")):
        rel = md_file.relative_to(project_dir)
        if _should_skip(rel):
            continue
        rel_str = rel.as_posix()
        try:
            safe_file = contained_path(project_dir, rel_str)
            parsed = parse_document(safe_file, project_key=project_key, project_root=project_dir)
            signature = stat_signature(safe_file) or (0, 0)
            _upsert_document(conn, parsed, mtime_ns=signature[0], size_bytes=signature[1])
            seen_paths.add(rel_str)
            result.documents_indexed += 1
        except (OSError, ValueError) as exc:
            result.errors += 1
            result.error_messages.append(f"{rel_str}: indexing failed ({type(exc).__name__})")

    rows = conn.execute(
        "SELECT path FROM documents WHERE project_key = ?", (project_key,)
    ).fetchall()
    for row in rows:
        if row["path"] not in seen_paths:
            conn.execute(
                "DELETE FROM documents WHERE project_key = ? AND path = ?",
                (project_key, row["path"]),
            )
            conn.execute(
                "DELETE FROM search_index WHERE project_key = ? AND path = ?",
                (project_key, row["path"]),
            )
            result.documents_removed += 1

    conn.commit()
    return result


def rebuild_index(
    conn: DbConnection,
    workspace_root: Path,
    project_keys: list[str],
    *,
    locks_held: bool = False,
) -> IndexResult:
    """Rebuild the derived index for the given projects from scratch."""
    if not locks_held:
        with ExitStack() as stack:
            for key in sorted(set(project_keys)):
                project_dir = project_dir_for(workspace_root, key)
                if project_dir.is_dir():
                    stack.enter_context(acquire_project_lock(project_dir, key))
            return rebuild_index(
                conn,
                workspace_root,
                project_keys,
                locks_held=True,
            )

    total = IndexResult()
    for key in project_keys:
        conn.execute("DELETE FROM documents WHERE project_key = ?", (key,))
        conn.execute("DELETE FROM search_index WHERE project_key = ?", (key,))
        conn.commit()
        sub = index_project(conn, workspace_root, key)
        total.documents_indexed += sub.documents_indexed
        total.documents_removed += sub.documents_removed
        total.errors += sub.errors
        total.error_messages.extend(sub.error_messages)
    return total


def get_indexed_signature(
    conn: DbConnection, project_key: str, path: str
) -> tuple[int, int, str] | None:
    """Return ``(mtime_ns, size_bytes, sha256)`` from the index, if present."""
    row = conn.execute(
        "SELECT mtime_ns, size_bytes, sha256 FROM documents WHERE project_key = ? AND path = ?",
        (project_key, path),
    ).fetchone()
    if row is None:
        return None
    return int(row["mtime_ns"]), int(row["size_bytes"]), str(row["sha256"])


def _upsert_document(
    conn: DbConnection,
    parsed: ParsedDocument,
    *,
    mtime_ns: int,
    size_bytes: int,
) -> None:
    now = _now_iso()
    conn.execute(
        """INSERT OR REPLACE INTO documents
           (project_key, path, id, title, folder, status, edit_policy,
            frontmatter_json, sha256, mtime_ns, size_bytes,
            created_at, updated_at, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            parsed.project_key,
            parsed.path,
            parsed.id,
            parsed.title,
            parsed.folder,
            parsed.status,
            parsed.edit_policy,
            json.dumps(parsed.frontmatter),
            parsed.sha256,
            mtime_ns,
            size_bytes,
            parsed.created_at or now,
            parsed.updated_at or now,
            now,
        ),
    )
    conn.execute(
        "DELETE FROM search_index WHERE project_key = ? AND path = ?",
        (parsed.project_key, parsed.path),
    )
    conn.execute(
        "INSERT INTO search_index (title, body, project_key, path) VALUES (?, ?, ?, ?)",
        (parsed.title, parsed.body, parsed.project_key, parsed.path),
    )
