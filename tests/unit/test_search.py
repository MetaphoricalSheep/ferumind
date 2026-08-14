"""Tests for section-aware FTS5 search (RET-03 / spec-versioning §2.3)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from ferumind.core.document_map import build_document_map, read_document_range
from ferumind.core.document_writes import create_document
from ferumind.core.errors import ValidationError
from ferumind.core.frontmatter import generate_frontmatter
from ferumind.core.lifecycle_writes import archive_document
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.project_writes import create_project
from ferumind.core.reconcile import reconcile_project
from ferumind.core.search import (
    MAX_SEARCH_QUERY_CHARS,
    MAX_SEARCH_RESULTS,
    MAX_SEARCH_TERMS,
    SearchResult,
    build_match_expression,
    search_project,
)
from tests.conftest import TEST_DESCRIPTION


def test_build_match_expression_quotes_terms_joined_with_or() -> None:
    assert build_match_expression("bench press") == '"bench" OR "press"'
    assert build_match_expression('"bench press" heavy') == '"bench press" OR "heavy"'
    # FTS operators are neutralized by quoting; OR is a join, not user syntax.
    assert build_match_expression("NEAR(x) OR *") == '"NEAR(x)" OR "OR" OR "*"'


def test_build_match_expression_rejects_unbalanced_quotes() -> None:
    with pytest.raises(ValidationError, match="Unbalanced"):
        build_match_expression('"unclosed phrase')
    with pytest.raises(ValidationError, match="at least one term"):
        build_match_expression('""')


def test_build_match_expression_rejects_query_and_term_bounds() -> None:
    with pytest.raises(ValidationError, match="character limit"):
        build_match_expression("x" * (MAX_SEARCH_QUERY_CHARS + 1))
    with pytest.raises(ValidationError, match="term limit"):
        build_match_expression(" ".join(f"t{i}" for i in range(MAX_SEARCH_TERMS + 1)))


def _project_file(workspace: WorkspaceRoot, project: str, rel: str) -> Path:
    return workspace / "projects" / project / rel


def _write_oob(workspace: WorkspaceRoot, project: str, rel: str, content: str) -> Path:
    path = _project_file(workspace, project, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _bump_mtime(path: Path) -> None:
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000))


def _seed(conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str) -> None:
    create_document(
        conn,
        workspace,
        project,
        folder_path="canvases",
        title="Training Log",
        content="Bench pressed 3x8 at 20 kg. Felt strong.\n",
        description=TEST_DESCRIPTION,
    )
    create_document(
        conn,
        workspace,
        project,
        folder_path="library",
        title="Bench Guide",
        content="A guide about bench pressing form, bench setup, bench arch.\n",
        description=TEST_DESCRIPTION,
    )
    create_document(
        conn,
        workspace,
        project,
        folder_path="memory",
        title="Unrelated",
        content="Squats only here.\n",
        description=TEST_DESCRIPTION,
    )


def _assert_section_shape(hit: SearchResult) -> None:
    dumped = hit.model_dump()
    assert "edit_policy" not in dumped
    assert hit.kind in {"preamble", "heading"}
    assert hit.section_id
    assert hit.start_line >= 1
    assert hit.end_line >= hit.start_line
    assert hit.size_bytes >= 0
    assert isinstance(hit.heading_path, list)
    assert hit.folder
    assert hit.status


def test_search_returns_ranked_section_hits_with_real_scores(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _seed(conn, workspace, project)
    results = search_project(conn, project, "bench")
    assert results
    for hit in results:
        _assert_section_shape(hit)
    assert [r.path for r in results][:2] == [
        "library/bench-guide.md",
        "canvases/training-log.md",
    ]
    assert all(r.score != 0.0 for r in results)
    assert results[0].score >= results[1].score
    assert "[bench]" in results[0].snippet.lower()


def test_search_stems_via_porter(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _seed(conn, workspace, project)
    # "pressing" stems to match "pressed".
    results = search_project(conn, project, "pressing")
    assert any(r.path == "canvases/training-log.md" for r in results)


def test_search_finds_exact_identifiers_and_literals(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    create_document(
        conn,
        workspace,
        project,
        folder_path="canvases",
        title="Station Notes",
        content="Station AWS-07 fault code E-RADIO-CRC token xyzzy9q.\n",
        description=TEST_DESCRIPTION,
    )
    for query in ("AWS-07", "E-RADIO-CRC", "xyzzy9q"):
        hits = search_project(conn, project, query)
        assert hits, f"expected a hit for {query!r}"
        assert any(query in hit.snippet or query.lower() in hit.snippet.lower() for hit in hits)


def test_search_matches_quoted_phrases(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    create_document(
        conn,
        workspace,
        project,
        folder_path="memory",
        title="Phrase Log",
        content="## Log\n\nThe quick brown fox jumps.\n\nOther noise about brown alone.\n",
        description=TEST_DESCRIPTION,
    )
    hits = search_project(conn, project, '"quick brown fox"')
    assert len(hits) == 1
    assert hits[0].path == "memory/phrase-log.md"
    assert "quick brown fox" in hits[0].snippet.lower()
    assert "[" in hits[0].snippet
    assert "]" in hits[0].snippet


def test_search_hit_in_document_title(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    create_document(
        conn,
        workspace,
        project,
        folder_path="canvases",
        title="Quokka Manual",
        content="## Setup\n\nOnly setup details here, no distinctive title word.\n",
        description=TEST_DESCRIPTION,
    )
    hits = search_project(conn, project, "Quokka")
    assert hits
    assert any(hit.path == "canvases/quokka-manual.md" and "Quokka" in hit.title for hit in hits)


def test_search_hit_in_heading(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    create_document(
        conn,
        workspace,
        project,
        folder_path="library",
        title="Guide",
        content="## Wombat Protocol\n\nGeneric prose without the keyword.\n",
        description=TEST_DESCRIPTION,
    )
    hits = search_project(conn, project, "Wombat")
    assert hits
    hit = next(h for h in hits if h.path == "library/guide.md")
    assert hit.kind == "heading"
    assert hit.heading_text is not None
    assert "Wombat" in hit.heading_text
    assert hit.level == 2


def test_search_hit_in_section_body(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    create_document(
        conn,
        workspace,
        project,
        folder_path="canvases",
        title="Body Match",
        content="## Setup\n\nThe body contains platypus evidence only here.\n",
        description=TEST_DESCRIPTION,
    )
    hits = search_project(conn, project, "platypus")
    assert hits
    hit = hits[0]
    assert hit.path == "canvases/body-match.md"
    assert hit.kind == "heading"
    assert hit.heading_text == "Setup"
    assert "platypus" in hit.snippet.lower()


def test_search_buried_evidence_returns_that_section(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    parts = ["## Intro\n\nnoise\n"]
    for index in range(20):
        parts.append(f"## Section {index}\n\nplaceholder text number {index}\n")
    parts.append("## Buried Vault\n\nThe unique evidence is ZORBLAT-99 only here.\n")
    create_document(
        conn,
        workspace,
        project,
        folder_path="library",
        title="Large Doc",
        content="\n".join(parts),
        description=TEST_DESCRIPTION,
    )
    hits = search_project(conn, project, "ZORBLAT-99")
    assert len(hits) == 1
    hit = hits[0]
    assert hit.path == "library/large-doc.md"
    assert hit.section_id == "buried-vault"
    assert hit.heading_text == "Buried Vault"
    assert hit.start_line < hit.end_line or hit.start_line == hit.end_line

    content = _project_file(workspace, project, hit.path).read_text(encoding="utf-8")
    ranged = read_document_range(
        content=content,
        project_key=project,
        path=hit.path,
        start_line=hit.start_line,
        end_line=hit.end_line,
    )
    assert "ZORBLAT-99" in ranged.content


def test_search_multiple_matching_sections_within_one_document(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    create_document(
        conn,
        workspace,
        project,
        folder_path="canvases",
        title="Multi",
        content=(
            "## Alpha\n"
            "alpha unique term xylophone here\n"
            "\n"
            "## Beta\n"
            "beta unique term xylophone also here\n"
            "\n"
            "## Gamma\n"
            "unrelated text\n"
        ),
        description=TEST_DESCRIPTION,
    )
    hits = search_project(conn, project, "xylophone")
    paths = [hit.path for hit in hits]
    assert paths.count("canvases/multi.md") >= 2
    section_ids = {hit.section_id for hit in hits if hit.path == "canvases/multi.md"}
    assert section_ids == {"alpha", "beta"}
    # Several sections from one doc appear as separate hits — do not collapse.
    assert len([hit for hit in hits if hit.path == "canvases/multi.md"]) == 2


def test_search_matching_sections_across_documents(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    create_document(
        conn,
        workspace,
        project,
        folder_path="canvases",
        title="Doc One",
        content="## A\n\nsharedterm in first document\n",
        description=TEST_DESCRIPTION,
    )
    create_document(
        conn,
        workspace,
        project,
        folder_path="library",
        title="Doc Two",
        content="## B\n\nsharedterm in second document\n",
        description=TEST_DESCRIPTION,
    )
    hits = search_project(conn, project, "sharedterm")
    assert {hit.path for hit in hits} == {"canvases/doc-one.md", "library/doc-two.md"}


def test_search_filters_by_folder_and_status(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _seed(conn, workspace, project)
    create_document(
        conn,
        workspace,
        project,
        folder_path="inbox",
        title="Gated Bench",
        content="Bench notes behind a gate.\n",
        status="gated",
        description=TEST_DESCRIPTION,
    )
    results = search_project(conn, project, "bench", folder="library")
    assert [r.path for r in results] == ["library/bench-guide.md"]
    assert all(r.folder == "library" for r in results)

    active = search_project(conn, project, "bench", status="active")
    assert {r.path for r in active} == {
        "library/bench-guide.md",
        "canvases/training-log.md",
    }
    gated = search_project(conn, project, "bench", status="gated")
    assert [r.path for r in gated] == ["inbox/gated-bench.md"]
    assert all(r.status == "gated" for r in gated)


def test_search_excludes_archived_by_default(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _seed(conn, workspace, project)
    archive_document(conn, workspace, project, path="library/bench-guide.md")
    results = search_project(conn, project, "bench")
    assert [r.path for r in results] == ["canvases/training-log.md"]
    included = search_project(conn, project, "bench", include_archived=True)
    assert {r.path for r in included} == {
        "canvases/training-log.md",
        "archive/library/bench-guide.md",
    }


def test_search_empty_query_returns_nothing(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    assert search_project(conn, project, "   ") == []
    assert search_project(conn, project, "") == []


def test_search_rejects_invalid_filters(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    with pytest.raises(ValidationError, match="folder"):
        search_project(conn, project, "bench", folder="secrets")
    with pytest.raises(ValidationError, match="status"):
        search_project(conn, project, "bench", status="unknown")
    with pytest.raises(ValidationError, match="limit"):
        search_project(conn, project, "bench", limit=MAX_SEARCH_RESULTS + 1)
    with pytest.raises(ValidationError, match="limit"):
        search_project(conn, project, "bench", limit=0)


def test_search_query_length_and_term_count_bounds(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    with pytest.raises(ValidationError, match="character limit"):
        search_project(conn, project, "x" * (MAX_SEARCH_QUERY_CHARS + 1))
    with pytest.raises(ValidationError, match="term limit"):
        search_project(
            conn,
            project,
            " ".join(f"term{i}" for i in range(MAX_SEARCH_TERMS + 1)),
        )


def test_search_limit_bounds_sections_not_documents(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    create_document(
        conn,
        workspace,
        project,
        folder_path="canvases",
        title="Many Hits",
        content=(
            "## One\nlimitmarker here\n\n## Two\nlimitmarker here\n\n## Three\nlimitmarker here\n"
        ),
        description=TEST_DESCRIPTION,
    )
    limited = search_project(conn, project, "limitmarker", limit=2)
    assert len(limited) == 2
    assert all(hit.path == "canvases/many-hits.md" for hit in limited)


def test_search_range_feeds_read_document_range_and_section_resolves_in_map(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    create_document(
        conn,
        workspace,
        project,
        folder_path="canvases",
        title="Ladder",
        content=(
            "## Early\n"
            "noise\n"
            "\n"
            "## Evidence\n"
            "The matched phrase is kangaroo-ledger.\n"
            "\n"
            "## Later\n"
            "more noise\n"
        ),
        description=TEST_DESCRIPTION,
    )
    hits = search_project(conn, project, "kangaroo-ledger")
    assert hits
    hit = hits[0]
    content = _project_file(workspace, project, hit.path).read_text(encoding="utf-8")

    ranged = read_document_range(
        content=content,
        project_key=project,
        path=hit.path,
        start_line=hit.start_line,
        end_line=hit.end_line,
    )
    assert "kangaroo-ledger" in ranged.content

    doc_map = build_document_map(content=content, project_key=project, path=hit.path)
    section_ids = {section.section_id for section in doc_map.sections}
    assert hit.section_id in section_ids
    mapped = next(section for section in doc_map.sections if section.section_id == hit.section_id)
    assert mapped.start_line == hit.start_line
    assert mapped.end_line == hit.end_line


def test_search_project_scope_has_no_cross_project_leakage(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    create_project(conn, workspace, key="other", title="Other")
    create_document(
        conn,
        workspace,
        project,
        folder_path="memory",
        title="Local",
        content="## Y\n\nunique crossleak term\n",
        description=TEST_DESCRIPTION,
    )
    create_document(
        conn,
        workspace,
        "other",
        folder_path="canvases",
        title="Secret",
        content="## X\n\nunique crossleak term\n",
        description=TEST_DESCRIPTION,
    )
    demo_hits = search_project(conn, project, "crossleak")
    other_hits = search_project(conn, "other", "crossleak")
    assert {hit.path for hit in demo_hits} == {"memory/local.md"}
    assert {hit.path for hit in other_hits} == {"canvases/secret.md"}
    assert all(hit.path != "canvases/secret.md" for hit in demo_hits)
    assert all(hit.path != "memory/local.md" for hit in other_hits)


def test_search_observes_out_of_band_edit_after_reconcile(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rel = "canvases/oob-edit.md"
    fm = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_oob_edit", project_key=project, title="OOB Edit"
    )
    path = _write_oob(workspace, project, rel, fm + "## First\n\noldmarkerterm\n")
    reconcile_project(conn, workspace, project)
    assert any(hit.path == rel for hit in search_project(conn, project, "oldmarkerterm"))

    path.write_text(fm + "## First\n\nnewmarkerterm\n", encoding="utf-8")
    _bump_mtime(path)
    reconcile_project(conn, workspace, project)

    assert search_project(conn, project, "oldmarkerterm") == []
    new_hits = search_project(conn, project, "newmarkerterm")
    assert any(hit.path == rel for hit in new_hits)
    assert any("newmarkerterm" in hit.snippet.lower() for hit in new_hits)


def test_search_indexes_out_of_band_new_file_after_reconcile(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rel = "canvases/oob-new.md"
    fm = generate_frontmatter(
        description=TEST_DESCRIPTION, doc_id="doc_oob_new", project_key=project, title="OOB New"
    )
    _write_oob(workspace, project, rel, fm + "## Fresh\n\nbrandnewsearchabletoken\n")
    assert search_project(conn, project, "brandnewsearchabletoken") == []

    reconcile_project(conn, workspace, project)
    hits = search_project(conn, project, "brandnewsearchabletoken")
    assert len(hits) == 1
    assert hits[0].path == rel
    assert hits[0].section_id == "fresh"


def test_search_drops_out_of_band_deleted_file_after_reconcile(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    created = create_document(
        conn,
        workspace,
        project,
        folder_path="canvases",
        title="OOB Delete",
        content="## Keep\n\ndeletablemarkerterm\n",
        description=TEST_DESCRIPTION,
    )
    assert any(
        hit.path == created.path for hit in search_project(conn, project, "deletablemarkerterm")
    )

    _project_file(workspace, project, created.path).unlink()
    reconcile_project(conn, workspace, project)
    assert search_project(conn, project, "deletablemarkerterm") == []
