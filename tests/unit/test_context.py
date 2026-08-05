"""Tests for the get_context assembly (spec-mcp §4)."""

from __future__ import annotations

import sqlite3

from ferumind.core.context import build_context
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.registry import require_project
from ferumind.core.writes import archive_document, capture_note, create_document


def _context(conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str):
    entry = require_project(workspace, project)
    return build_context(conn, workspace, entry)


def test_rules_concatenate_workspace_then_project_with_headers(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    context = _context(conn, workspace, project)
    sources = context.rules.sources
    assert sources[:4] == [
        "system/rules/00-contract.md",
        "system/rules/10-editing.md",
        "system/rules/20-memory.md",
        "system/rules/30-reminders.md",
    ]
    assert sources[4] == f"projects/{project}/rules/00-project.md"
    # Each file is prefixed by an H2 header naming its source path.
    for source in sources:
        assert f"## {source}" in context.rules.content_markdown
    # No semantic merging: workspace rules text appears verbatim.
    assert context.rules.content_markdown.index("system/rules/00-contract.md") < (
        context.rules.content_markdown.index(f"projects/{project}/rules/00-project.md")
    )


def test_spine_and_payload_telemetry(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    context = _context(conn, workspace, project)
    assert context.spine is not None
    assert not context.spine_missing
    assert context.spine.path == "spine.md"
    assert context.payload.format == 2
    assert context.payload.spine_bytes == len(context.spine.content_markdown.encode("utf-8"))
    assert context.payload.rules_bytes == len(context.rules.content_markdown.encode("utf-8"))
    assert context.payload.documents_count == len(context.documents)


def test_spine_missing_is_flagged(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    (workspace / "projects" / project / "spine.md").unlink()
    context = _context(conn, workspace, project)
    assert context.spine is None
    assert context.spine_missing
    assert context.payload.spine_bytes == 0


def test_documents_exclude_archive_and_inbox_but_include_rules_memory(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    create_document(conn, workspace, project, folder_path="memory", title="Notes", content="n\n")
    doc = create_document(
        conn, workspace, project, folder_path="canvases", title="Old", content="o\n"
    )
    capture_note(conn, workspace, project, text="inbox item")
    archive_document(conn, workspace, project, path=doc.path)

    context = _context(conn, workspace, project)
    folders = {d.folder for d in context.documents}
    assert "rules" in folders
    assert "memory" in folders
    assert "archive" not in folders
    assert "inbox" not in folders
    assert all(d.status != "archived" for d in context.documents)
    assert context.inbox_count == 1


def test_context_reconciles_hand_made_files(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    hand_made = workspace / "projects" / project / "canvases" / "hand.md"
    hand_made.parent.mkdir(parents=True, exist_ok=True)
    hand_made.write_text("# Hand-made\n", encoding="utf-8")
    context = _context(conn, workspace, project)
    assert any(d.path == "canvases/hand.md" for d in context.documents)
