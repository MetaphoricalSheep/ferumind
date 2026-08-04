"""Tests for the derived index (documents table + FTS mirror)."""

from __future__ import annotations

import sqlite3

from lattice.core.frontmatter import generate_frontmatter
from lattice.core.indexer import (
    get_indexed_signature,
    index_file,
    index_project,
    rebuild_index,
    remove_from_index,
)
from lattice.core.paths import WorkspaceRoot


def _write_doc(workspace: WorkspaceRoot, project: str, rel: str, title: str) -> None:
    path = workspace / "projects" / project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = generate_frontmatter(doc_id=f"doc_{title.lower()}", project_key=project, title=title)
    path.write_text(fm + f"# {title}\n\ncontent for {title}\n", encoding="utf-8")


def test_index_project_indexes_and_prunes(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _write_doc(workspace, project, "canvases/a.md", "Alpha")
    _write_doc(workspace, project, "memory/b.md", "Beta")
    result = index_project(conn, workspace, project)
    # spine + rules seed + the two new docs
    assert result.documents_indexed == 4
    assert result.errors == 0

    row = conn.execute(
        "SELECT folder, status, edit_policy FROM documents WHERE project_key = ? AND path = ?",
        (project, "canvases/a.md"),
    ).fetchone()
    assert (row["folder"], row["status"], row["edit_policy"]) == ("canvases", "active", "free")

    # Delete a file on disk; re-indexing prunes its rows.
    (workspace / "projects" / project / "memory/b.md").unlink()
    result = index_project(conn, workspace, project)
    assert result.documents_removed == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM search_index WHERE project_key = ? AND path = ?",
            (project, "memory/b.md"),
        ).fetchone()[0]
        == 0
    )


def test_reindexing_does_not_accumulate_rows(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _write_doc(workspace, project, "canvases/a.md", "Alpha")
    index_project(conn, workspace, project)
    index_project(conn, workspace, project)
    count = conn.execute(
        "SELECT COUNT(*) FROM search_index WHERE project_key = ? AND path = ?",
        (project, "canvases/a.md"),
    ).fetchone()[0]
    assert count == 1


def test_index_file_records_stat_signature(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _write_doc(workspace, project, "canvases/a.md", "Alpha")
    file_path = workspace / "projects" / project / "canvases/a.md"
    parsed = index_file(conn, workspace, project, file_path)
    signature = get_indexed_signature(conn, project, "canvases/a.md")
    assert signature is not None
    mtime_ns, size_bytes, sha256 = signature
    stat = file_path.stat()
    assert (mtime_ns, size_bytes) == (stat.st_mtime_ns, stat.st_size)
    assert sha256 == parsed.sha256


def test_remove_from_index(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _write_doc(workspace, project, "canvases/a.md", "Alpha")
    index_project(conn, workspace, project)
    remove_from_index(conn, project, "canvases/a.md")
    assert get_indexed_signature(conn, project, "canvases/a.md") is None


def test_index_skips_hidden_and_lattice_internals(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    hidden = workspace / "projects" / project / ".lattice" / "snapshots" / "x.md"
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text("# internal\n", encoding="utf-8")
    index_project(conn, workspace, project)
    assert get_indexed_signature(conn, project, ".lattice/snapshots/x.md") is None


def test_index_records_errors_for_unparseable_documents(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    bad = workspace / "projects" / project / "canvases" / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(
        "---\nid: doc_bad\ntype: document\nproject: demo\nstatus: bogus\n---\n", encoding="utf-8"
    )
    result = index_project(conn, workspace, project)
    assert result.errors == 1
    assert any("bad.md" in message for message in result.error_messages)


def test_rebuild_index_from_scratch(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _write_doc(workspace, project, "canvases/a.md", "Alpha")
    index_project(conn, workspace, project)
    # Poison the index; rebuild must converge back to disk truth.
    conn.execute("DELETE FROM documents WHERE project_key = ?", (project,))
    conn.commit()
    result = rebuild_index(conn, workspace, [project])
    assert result.documents_indexed == 3  # spine + rules seed + a.md
    assert get_indexed_signature(conn, project, "canvases/a.md") is not None
