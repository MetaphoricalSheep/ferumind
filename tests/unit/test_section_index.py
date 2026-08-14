"""Section-level derived index: shared parser, maintenance, lifecycle."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from ferumind.core import document_map as document_map_core
from ferumind.core.document_map import build_document_map
from ferumind.core.document_writes import create_document
from ferumind.core.errors import RangeTooLargeError
from ferumind.core.frontmatter import generate_frontmatter
from ferumind.core.indexer import (
    index_file,
    index_project,
    rebuild_index,
    remove_from_index,
)
from ferumind.core.lifecycle_writes import archive_document, unarchive_document
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.reconcile import reconcile_document
from tests.conftest import TEST_DESCRIPTION


def _write_raw(workspace: WorkspaceRoot, project: str, rel: str, content: str) -> Path:
    path = workspace / "projects" / project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _section_rows(conn: sqlite3.Connection, project: str, path: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT section_id, kind, heading_text, heading_path_json, level,
                      start_line, end_line, content_sha256, size_bytes, body
               FROM section_index
               WHERE project_key = ? AND path = ?
               ORDER BY CAST(start_line AS INTEGER), section_id""",
            (project, path),
        ).fetchall()
    )


def _assert_map_matches_index(
    conn: sqlite3.Connection,
    *,
    project: str,
    path: str,
    content: str,
) -> None:
    doc_map = build_document_map(content=content, project_key=project, path=path)
    indexed = _section_rows(conn, project, path)
    assert len(indexed) == len(doc_map.sections)
    for row, section in zip(indexed, doc_map.sections, strict=True):
        assert row["section_id"] == section.section_id
        assert row["kind"] == section.kind
        assert (row["heading_text"] or None) == section.heading_text
        assert json.loads(row["heading_path_json"]) == section.heading_path
        level = None if row["level"] in (None, "") else int(row["level"])
        assert level == section.level
        assert int(row["start_line"]) == section.start_line
        assert int(row["end_line"]) == section.end_line
        assert row["content_sha256"] == section.content_sha256
        expected_body = "\n".join(content.split("\n")[section.start_line - 1 : section.end_line])
        assert row["body"] == expected_body
        assert int(row["size_bytes"]) == len(expected_body.encode("utf-8"))
        assert section.size_bytes == int(row["size_bytes"])


def _assert_no_duplicates(conn: sqlite3.Connection, project: str, path: str) -> None:
    rows = conn.execute(
        """SELECT section_id, COUNT(*) AS n
           FROM section_index
           WHERE project_key = ? AND path = ?
           GROUP BY section_id
           HAVING n > 1""",
        (project, path),
    ).fetchall()
    assert rows == []


CASES: dict[str, str] = {
    "preamble_and_headings": generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_preamble", project_key="demo", title="Preamble"
    )
    + (
        "intro paragraph\n"
        "\n"
        "# Title\n"
        "\n"
        "## Section One\n"
        "\n"
        "text one\n"
        "\n"
        "```python\n"
        "# a comment heading in code\n"
        "```\n"
        "\n"
        "## Section Two\n"
        "\n"
        "text two\n"
    ),
    "nested": generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_nested", project_key="demo", title="Nested"
    )
    + "# Root\n\n## Child\n\n### Grandchild\n\nbody\n",
    "repeated_headings": generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_repeat", project_key="demo", title="Repeat"
    )
    + "# Same\n\none\n\n# Same\n\ntwo\n\n# Same\n\nthree\n",
    "no_headings": generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_plain", project_key="demo", title="Plain"
    )
    + "just a paragraph\nand another\n",
    "empty_body": generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_empty", project_key="demo", title="Empty"
    ),
    "fenced_only": generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_fence", project_key="demo", title="Fence"
    )
    + "before\n\n```\n# not a heading\n```\n\nafter\n",
    "no_frontmatter": "# Bare Title\n\nbody without frontmatter\n",
}


@pytest.mark.parametrize("case_name", sorted(CASES))
def test_indexed_sections_match_document_map(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    case_name: str,
) -> None:
    rel = f"canvases/{case_name}.md"
    content = CASES[case_name]
    _write_raw(workspace, project, rel, content)
    index_project(conn, workspace, project)
    _assert_map_matches_index(conn, project=project, path=rel, content=content)
    _assert_no_duplicates(conn, project, rel)


def test_indexing_ignores_map_line_caps(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Over MAX_LINES_IN_MAP (5000): map with include_lines would refuse, but
    # indexing and a normal map (without include_lines) both succeed.
    lines = ["# Title"] + [f"line {i}" for i in range(6_000)]
    content = "\n".join(lines) + "\n"
    rel = "canvases/large.md"
    _write_raw(workspace, project, rel, content)
    result = index_project(conn, workspace, project)
    assert result.errors == 0
    _assert_map_matches_index(conn, project=project, path=rel, content=content)

    monkeypatch.setattr(document_map_core, "MAX_STRUCTURE_LINES", 3)
    with pytest.raises(RangeTooLargeError):
        build_document_map(content=content, project_key=project, path=rel)
    # Indexing still produced rows for the large document.
    assert _section_rows(conn, project, rel)


def test_heading_rename_refreshes_section_id(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rel = "canvases/rename.md"
    original = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_rename", project_key="demo", title="Rename"
    ) + ("# Alpha\n\nbody\n")
    _write_raw(workspace, project, rel, original)
    index_project(conn, workspace, project)
    assert {row["section_id"] for row in _section_rows(conn, project, rel)} == {"alpha"}

    renamed = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_rename", project_key="demo", title="Rename"
    ) + ("# Beta\n\nbody\n")
    _write_raw(workspace, project, rel, renamed)
    index_project(conn, workspace, project)
    ids = {row["section_id"] for row in _section_rows(conn, project, rel)}
    assert ids == {"beta"}
    _assert_map_matches_index(conn, project=project, path=rel, content=renamed)
    _assert_no_duplicates(conn, project, rel)


def test_heading_level_change_and_section_insert_delete(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rel = "canvases/structure.md"
    base = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_struct", project_key="demo", title="Struct"
    ) + ("# One\n\nbody one\n\n## Two\n\nbody two\n")
    _write_raw(workspace, project, rel, base)
    index_project(conn, workspace, project)

    level_changed = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_struct", project_key="demo", title="Struct"
    ) + ("## One\n\nbody one\n\n## Two\n\nbody two\n")
    _write_raw(workspace, project, rel, level_changed)
    index_project(conn, workspace, project)
    levels = {
        row["section_id"]: None if row["level"] in (None, "") else int(row["level"])
        for row in _section_rows(conn, project, rel)
    }
    assert levels["one"] == 2
    _assert_map_matches_index(conn, project=project, path=rel, content=level_changed)

    with_insert = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_struct", project_key="demo", title="Struct"
    ) + ("## One\n\nbody one\n\n## Two\n\nbody two\n\n## Three\n\nbody three\n")
    _write_raw(workspace, project, rel, with_insert)
    index_project(conn, workspace, project)
    assert {row["section_id"] for row in _section_rows(conn, project, rel)} == {
        "one",
        "two",
        "three",
    }

    with_delete = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_struct", project_key="demo", title="Struct"
    ) + ("## One\n\nbody one\n\n## Three\n\nbody three\n")
    _write_raw(workspace, project, rel, with_delete)
    index_project(conn, workspace, project)
    assert {row["section_id"] for row in _section_rows(conn, project, rel)} == {
        "one",
        "three",
    }
    _assert_map_matches_index(conn, project=project, path=rel, content=with_delete)
    _assert_no_duplicates(conn, project, rel)


def test_body_change_keeps_structure_and_updates_hash(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rel = "canvases/body.md"
    before = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_body", project_key="demo", title="Body"
    ) + ("# Title\n\nold body\n")
    _write_raw(workspace, project, rel, before)
    index_project(conn, workspace, project)
    before_hash = _section_rows(conn, project, rel)[0]["content_sha256"]

    after = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_body", project_key="demo", title="Body"
    ) + ("# Title\n\nnew body\n")
    _write_raw(workspace, project, rel, after)
    index_project(conn, workspace, project)
    rows = _section_rows(conn, project, rel)
    assert len(rows) == 1
    assert rows[0]["section_id"] == "title"
    assert rows[0]["content_sha256"] != before_hash
    _assert_map_matches_index(conn, project=project, path=rel, content=after)


def test_document_deletion_and_remove_from_index(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rel = "canvases/gone.md"
    content = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_gone", project_key="demo", title="Gone"
    ) + ("# Gone\n\nbody\n")
    path = _write_raw(workspace, project, rel, content)
    index_project(conn, workspace, project)
    assert _section_rows(conn, project, rel)

    path.unlink()
    result = index_project(conn, workspace, project)
    assert result.documents_removed >= 1
    assert _section_rows(conn, project, rel) == []

    path = _write_raw(workspace, project, rel, content)
    index_file(conn, workspace, project, path)
    assert _section_rows(conn, project, rel)
    remove_from_index(conn, project, rel)
    assert _section_rows(conn, project, rel) == []


def test_archive_and_unarchive_move_section_rows(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    created = create_document(
        conn,
        workspace,
        project,
        folder_path="canvases",
        title="Archive Me",
        content="# Archive Me\n\nsection body\n",
        description=TEST_DESCRIPTION,
    )
    path = created.path
    assert _section_rows(conn, project, path)

    archived = archive_document(conn, workspace, project, path=path)
    assert _section_rows(conn, project, path) == []
    assert _section_rows(conn, project, archived.archived_path)
    _assert_no_duplicates(conn, project, archived.archived_path)

    restored = unarchive_document(conn, workspace, project, archived_path=archived.archived_path)
    assert _section_rows(conn, project, archived.archived_path) == []
    assert _section_rows(conn, project, restored.path)
    _assert_no_duplicates(conn, project, restored.path)


def test_create_document_refreshes_sections_inline(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    created = create_document(
        conn,
        workspace,
        project,
        folder_path="memory",
        title="Inline Note",
        content="# Inline Note\n\nhello\n",
        description=TEST_DESCRIPTION,
    )
    # No direct index_project call — write path must have refreshed sections.
    rows = _section_rows(conn, project, created.path)
    assert rows
    content = (workspace / "projects" / project / created.path).read_text(encoding="utf-8")
    _assert_map_matches_index(conn, project=project, path=created.path, content=content)


def test_out_of_band_edit_and_delete_refresh_sections(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rel = "canvases/oob.md"
    original = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_oob", project_key="demo", title="OOB"
    ) + ("# Original\n\nbody\n")
    path = _write_raw(workspace, project, rel, original)
    index_project(conn, workspace, project)

    edited = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_oob", project_key="demo", title="OOB"
    ) + ("# Edited\n\nnew body\n")
    path.write_text(edited, encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000))
    outcome = reconcile_document(conn, workspace, project, rel)
    assert outcome.drifted
    assert {row["section_id"] for row in _section_rows(conn, project, rel)} == {"edited"}
    _assert_map_matches_index(conn, project=project, path=rel, content=edited)

    path.unlink()
    deleted = reconcile_document(conn, workspace, project, rel)
    assert deleted.removed
    assert _section_rows(conn, project, rel) == []


def test_rebuild_index_is_idempotent(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rel = "canvases/idempotent.md"
    content = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_idemp", project_key="demo", title="Idemp"
    ) + ("# One\n\nbody\n\n# Two\n\nmore\n")
    _write_raw(workspace, project, rel, content)
    rebuild_index(conn, workspace, [project])
    first = [
        (
            row["path"],
            row["section_id"],
            row["kind"],
            row["heading_text"],
            row["heading_path_json"],
            row["level"],
            row["start_line"],
            row["end_line"],
            row["content_sha256"],
            row["size_bytes"],
            row["body"],
        )
        for row in conn.execute(
            """SELECT path, section_id, kind, heading_text, heading_path_json, level,
                      start_line, end_line, content_sha256, size_bytes, body
               FROM section_index
               WHERE project_key = ?
               ORDER BY path, CAST(start_line AS INTEGER), section_id""",
            (project,),
        ).fetchall()
    ]
    rebuild_index(conn, workspace, [project])
    second = [
        (
            row["path"],
            row["section_id"],
            row["kind"],
            row["heading_text"],
            row["heading_path_json"],
            row["level"],
            row["start_line"],
            row["end_line"],
            row["content_sha256"],
            row["size_bytes"],
            row["body"],
        )
        for row in conn.execute(
            """SELECT path, section_id, kind, heading_text, heading_path_json, level,
                      start_line, end_line, content_sha256, size_bytes, body
               FROM section_index
               WHERE project_key = ?
               ORDER BY path, CAST(start_line AS INTEGER), section_id""",
            (project,),
        ).fetchall()
    ]
    assert first == second
    assert first  # seeded spine/rules plus our doc


def test_malformed_document_does_not_abort_project_walk(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    good = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_good", project_key="demo", title="Good"
    ) + ("# Good\n\nok\n")
    _write_raw(workspace, project, "canvases/good.md", good)
    bad = workspace / "projects" / project / "canvases" / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(
        "---\nid: doc_bad\ntype: document\nproject: demo\nstatus: bogus\n---\n",
        encoding="utf-8",
    )
    result = index_project(conn, workspace, project)
    assert result.errors == 1
    assert any("bad.md" in message for message in result.error_messages)
    assert _section_rows(conn, project, "canvases/good.md")
    assert _section_rows(conn, project, "canvases/bad.md") == []
