"""Tests for the get_context assembly (spec-mcp §4)."""

from __future__ import annotations

import sqlite3

from ferumind.core.context import build_context
from ferumind.core.document_writes import capture_note, create_document
from ferumind.core.folders import folder_of
from ferumind.core.format import SUPPORTED_FORMAT, meta_path, read_format, write_format_marker
from ferumind.core.lifecycle_writes import archive_document
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.registry import require_project
from tests.conftest import TEST_DESCRIPTION


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


def test_episode_contract_reaches_the_agent_through_merged_rules(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    """EPI-01's text is delivered on every call, attributed to the memory rules."""
    context = _context(conn, workspace, project)
    assert "memory/episodes/YYYY-MM.md" in context.rules.content_markdown
    assert "system/rules/20-memory.md" in context.rules.sources
    memory_section = context.rules.content_markdown.split("## system/rules/20-memory.md", 1)[1]
    next_source = memory_section.find("## system/rules/")
    if next_source != -1:
        memory_section = memory_section[:next_source]
    assert "memory/episodes/YYYY-MM.md" in memory_section


def test_source_grounding_reaches_the_agent_with_its_rule_sources(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    """PROV-01 is delivered by both contract files that own its guidance."""
    context = _context(conn, workspace, project)
    assert "system/rules/00-contract.md" in context.rules.sources
    assert "system/rules/20-memory.md" in context.rules.sources

    contract_section = context.rules.content_markdown.split("## system/rules/00-contract.md", 1)[
        1
    ].split("## system/rules/10-editing.md", 1)[0]
    memory_section = context.rules.content_markdown.split("## system/rules/20-memory.md", 1)[
        1
    ].split("## system/rules/30-reminders.md", 1)[0]

    assert "## Sources" in contract_section
    assert "curated-memory facts or inferences" in memory_section


def test_lookup_first_ladder_reaches_the_agent_through_merged_rules(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    """RET-04's progressive-disclosure ladder is delivered via get_context rules."""
    context = _context(conn, workspace, project)
    assert "skip the map when the hit is enough" in context.rules.content_markdown
    assert "system/rules/10-editing.md" in context.rules.sources
    editing_section = context.rules.content_markdown.split("## system/rules/10-editing.md", 1)[1]
    next_source = editing_section.find("## system/rules/")
    if next_source == -1:
        next_source = editing_section.find(f"## projects/{project}/")
    if next_source != -1:
        editing_section = editing_section[:next_source]
    assert "skip the map when the hit is enough" in editing_section
    assert "Never `read_document` first" in editing_section


def test_episode_month_file_needs_no_new_folder(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    """The premise the whole Episodes design rests on: ``memory/`` nests.

    ``memory/episodes/`` is a subdirectory of an existing role folder, so it
    needs no new top-level folder, no ``role:`` key, and no format bump.
    """
    result = create_document(
        conn,
        workspace,
        project,
        folder_path="memory/episodes",
        title="2026-08",
        content="## 2026-08-07 — Something happened\n",
        description=TEST_DESCRIPTION,
    )
    assert result.path == "memory/episodes/2026-08.md"
    assert folder_of(result.path) == "memory"

    context = _context(conn, workspace, project)
    recorded = [d for d in context.documents if d.path == "memory/episodes/2026-08.md"]
    assert len(recorded) == 1
    assert recorded[0].folder == "memory"


def test_spine_and_payload_telemetry(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    context = _context(conn, workspace, project)
    assert context.spine is not None
    assert not context.spine_missing
    assert context.spine.path == "spine.md"
    assert context.payload.format == read_format(workspace) == SUPPORTED_FORMAT
    assert context.payload.spine_bytes == len(context.spine.content_markdown.encode("utf-8"))
    assert context.payload.rules_bytes == len(context.rules.content_markdown.encode("utf-8"))
    assert context.payload.documents_count == len(context.documents)
    assert context.payload.descriptions_bytes == sum(
        len(document.description.encode("utf-8")) for document in context.documents
    )
    assert all(document.description for document in context.documents)
    assert all(document.size_bytes > 0 for document in context.documents)


def test_payload_echoes_the_older_workspace_format_that_was_read(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    old_format = SUPPORTED_FORMAT - 1
    write_format_marker(workspace, old_format)

    assert _context(conn, workspace, project).payload.format == old_format


def test_payload_does_not_invent_a_format_for_an_unreadable_marker(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    meta_path(workspace).write_text("format: not-a-number\n", encoding="utf-8")

    assert _context(conn, workspace, project).payload.format is None


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
    create_document(
        conn,
        workspace,
        project,
        folder_path="memory",
        title="Notes",
        content="n\n",
        description=TEST_DESCRIPTION,
    )
    doc = create_document(
        conn,
        workspace,
        project,
        folder_path="canvases",
        title="Old",
        content="o\n",
        description=TEST_DESCRIPTION,
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
