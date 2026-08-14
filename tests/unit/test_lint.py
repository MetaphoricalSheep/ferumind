"""Report-only workspace lint: deterministic findings, paths, and CLI behavior."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ferumind.cli.main import app
from ferumind.core.format import SUPPORTED_FORMAT, write_format_marker
from ferumind.core.lint import LintFinding, lint_workspace
from ferumind.core.paths import WorkspaceRoot, contained_project_root
from tests.conftest import managed_markdown

runner = CliRunner()


def _project_root(workspace: WorkspaceRoot, project: str) -> Path:
    return contained_project_root(workspace, project)


def _write(
    workspace: WorkspaceRoot,
    project: str,
    path: str,
    body: str,
    *,
    doc_id: str | None = None,
    description: str = "Synthetic lint fixture used to exercise one check.",
    status: str = "active",
    extra_frontmatter: str = "",
) -> Path:
    target = _project_root(workspace, project) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    markdown = managed_markdown(
        body,
        project_key=project,
        doc_id=doc_id or f"doc_{hashlib.sha256(path.encode()).hexdigest()[:12]}",
        title=Path(path).stem,
        description=description,
        extra_frontmatter=extra_frontmatter,
    )
    target.write_text(markdown.replace("status: active", f"status: {status}", 1), encoding="utf-8")
    return target


def _lint(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> list[LintFinding]:
    return lint_workspace(conn, workspace, [project]).findings


def _only(findings: list[LintFinding], check_id: str) -> LintFinding:
    assert [finding.check_id for finding in findings] == [check_id]
    return findings[0]


def test_clean_project_is_quiet(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    report = lint_workspace(conn, workspace, [project])

    assert report.findings == []
    assert report.summary.findings == 0
    assert report.documents_checked == 2
    assert report.links_checked == 0


def test_empty_selection_is_a_stable_empty_report(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    report = lint_workspace(conn, workspace, [])

    assert report.findings == []
    assert report.projects_checked == []
    assert report.documents_checked == 0
    assert report.links_checked == 0


@pytest.mark.parametrize("description", ["", "   "])
def test_missing_or_blank_description_is_one_specific_error(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    description: str,
) -> None:
    _write(workspace, project, "canvases/bad.md", "# Bad\n", description=description)

    finding = _only(_lint(conn, workspace, project), "invalid_description")
    assert finding.severity == "error"
    assert finding.path == "canvases/bad.md"


def test_invalid_frontmatter_is_reported_without_index_noise(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(
        workspace,
        project,
        "canvases/bad.md",
        "# Bad\n",
        status="impossible",
    )

    finding = _only(_lint(conn, workspace, project), "invalid_frontmatter")
    assert finding.path == "canvases/bad.md"


def test_previously_indexed_malformed_document_is_reported_without_crashing(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    target = _write(workspace, project, "canvases/bad.md", "# Initially valid\n")
    assert _lint(conn, workspace, project) == []
    target.write_text("---\nid: unterminated\n", encoding="utf-8")

    finding = _only(_lint(conn, workspace, project), "invalid_frontmatter")
    assert finding.path == "canvases/bad.md"


def test_previously_indexed_non_utf8_document_is_reported_without_crashing(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    target = _write(workspace, project, "canvases/bad.md", "# Initially valid\n")
    assert _lint(conn, workspace, project) == []
    target.write_bytes(b"\xff\xfe")

    finding = _only(_lint(conn, workspace, project), "invalid_frontmatter")
    assert "UTF-8" in finding.message


def test_frontmatter_markdown_shape_is_not_a_link(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(
        workspace,
        project,
        "canvases/source.md",
        "# No links\n",
        extra_frontmatter="note: '[Not body Markdown](missing.md)'",
    )

    assert _lint(conn, workspace, project) == []


def test_duplicate_managed_ids_name_every_duplicate_path(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(workspace, project, "canvases/one.md", "# One\n", doc_id="doc_duplicate")
    _write(workspace, project, "memory/two.md", "# Two\n", doc_id="doc_duplicate")

    findings = _lint(conn, workspace, project)

    assert [finding.check_id for finding in findings] == [
        "duplicate_document_id",
        "duplicate_document_id",
    ]
    assert {finding.path for finding in findings} == {"canvases/one.md", "memory/two.md"}


@pytest.mark.parametrize(
    ("destination", "check_id"),
    [
        ("missing.md", "broken_internal_link"),
        ("missing.pdf", "missing_file"),
        ("../../../outside.md", "unresolvable_link"),
        ("/absolute.md", "unresolvable_link"),
        ("../.ferumind/ferumind.sqlite", "unresolvable_link"),
        ("ferumind://file/demo/library/secret.pdf", "unresolvable_link"),
    ],
)
def test_broken_and_unsafe_links_are_distinguished(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    destination: str,
    check_id: str,
) -> None:
    _write(workspace, project, "canvases/source.md", f"[target]({destination})\n")

    finding = _only(_lint(conn, workspace, project), check_id)
    assert finding.line is not None
    assert finding.target == destination


def test_symlinked_link_target_is_never_followed(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (_project_root(workspace, project) / "library" / "escape.txt").symlink_to(outside)
    _write(workspace, project, "canvases/source.md", "[target](../library/escape.txt)\n")

    finding = _only(_lint(conn, workspace, project), "unresolvable_link")
    assert "symlinked" in finding.message
    assert "outside" not in finding.message


def test_malformed_external_url_is_not_validated(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(workspace, project, "canvases/source.md", "[external](https://[bad)\n")

    assert _lint(conn, workspace, project) == []


def test_link_resolving_only_through_archive_is_a_warning(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(workspace, project, "canvases/source.md", "[old](old.md)\n")
    _write(
        workspace,
        project,
        "archive/canvases/old.md",
        "# Old\n",
        status="archived",
    )

    finding = _only(_lint(conn, workspace, project), "archived_target")
    assert finding.severity == "warning"
    assert "archive/canvases/old.md" in finding.message


def test_archived_document_citing_a_live_file_is_not_reported(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    """An archived document can never be edited, so its stale links are unactionable."""
    live = _project_root(workspace, project) / "library" / "graphs" / "trend.png"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(b"\x89PNG\r\n\x1a\n")
    _write(
        workspace,
        project,
        "archive/canvases/reports/report.md",
        "![trend](../../library/graphs/trend.png)\n",
        status="archived",
    )

    assert _lint(conn, workspace, project) == []


def test_live_document_citing_an_unarchived_target_is_still_a_warning(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    """The live source is editable, so naming a stale archive path is repairable."""
    _write(workspace, project, "canvases/restored.md", "# Restored\n")
    _write(
        workspace,
        project,
        "canvases/source.md",
        "[restored](../archive/canvases/restored.md)\n",
    )

    finding = _only(_lint(conn, workspace, project), "archived_target")
    assert finding.severity == "warning"
    assert "outside the archive" in finding.message


def test_link_to_an_existing_directory_is_not_a_finding(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    """A spine document map legitimately names folders, not only documents."""
    _write(workspace, project, "canvases/logs/january.md", "# January\n")
    _write(workspace, project, "canvases/source.md", "[logs](logs/)\n")

    assert _lint(conn, workspace, project) == []


def test_link_to_a_missing_directory_is_still_reported(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(workspace, project, "canvases/source.md", "[logs](logs/)\n")

    finding = _only(_lint(conn, workspace, project), "missing_file")
    assert finding.severity == "error"


def test_sources_heading_gets_only_generic_link_validation(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(
        workspace,
        project,
        "canvases/source.md",
        "## Sources\n\n- [missing](missing.md)\n",
    )

    finding = _only(_lint(conn, workspace, project), "broken_internal_link")
    assert finding.line is not None


def test_illegal_role_folder_is_one_error(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(workspace, project, "unknown/bad.md", "# Bad\n")

    finding = _only(_lint(conn, workspace, project), "illegal_folder")
    assert finding.path == "unknown/bad.md"


def test_bad_internal_fragment_is_a_warning(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(workspace, project, "canvases/target.md", "# Real Heading\n")
    _write(workspace, project, "canvases/source.md", "[target](target.md#not-real)\n")

    finding = _only(_lint(conn, workspace, project), "invalid_fragment")
    assert finding.severity == "warning"


def test_valid_internal_fragment_is_quiet(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(workspace, project, "canvases/target.md", "# Real Heading\n")
    _write(workspace, project, "canvases/source.md", "[target](target.md#real-heading)\n")

    assert _lint(conn, workspace, project) == []


def test_reconcile_clears_normal_drift_before_index_check(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(workspace, project, "canvases/out-of-band.md", "# Out of band\n")

    assert _lint(conn, workspace, project) == []
    assert conn.execute(
        "SELECT 1 FROM documents WHERE project_key = ? AND path = ?",
        (project, "canvases/out-of-band.md"),
    ).fetchone()


def test_index_inconsistency_surviving_reconcile_is_an_error(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    assert _lint(conn, workspace, project) == []
    conn.execute(
        "DELETE FROM search_index WHERE project_key = ? AND path = ?",
        (project, "spine.md"),
    )
    conn.commit()

    finding = _only(_lint(conn, workspace, project), "index_inconsistency")
    assert finding.severity == "error"
    assert "search_index_count" in finding.message


def test_older_format_skips_description_check_but_keeps_independent_checks(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    write_format_marker(workspace, SUPPORTED_FORMAT - 1)
    target = _project_root(workspace, project) / "canvases" / "old.md"
    target.write_text(
        managed_markdown("[missing](missing.md)\n", project_key=project).replace(
            "description: Fixture document used by the Ferumind test suite.\n", ""
        ),
        encoding="utf-8",
    )

    findings = _lint(conn, workspace, project)

    assert [finding.check_id for finding in findings] == [
        "broken_internal_link",
        "workspace_format",
    ]
    assert all(finding.check_id != "invalid_description" for finding in findings)


def test_newer_format_fails_closed_after_one_format_finding(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    write_format_marker(workspace, SUPPORTED_FORMAT + 1)
    _write(workspace, project, "canvases/source.md", "[missing](missing.md)\n")

    finding = _only(_lint(conn, workspace, project), "workspace_format")
    assert finding.project == "workspace"


def test_two_runs_are_byte_for_byte_deterministic(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(workspace, project, "canvases/source.md", "[missing](missing.md)\n")

    first = lint_workspace(conn, workspace, [project]).model_dump_json()
    second = lint_workspace(conn, workspace, [project]).model_dump_json()

    assert first == second


def test_lint_changes_no_markdown_bytes(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(workspace, project, "canvases/source.md", "[missing](missing.md)\n")
    root = _project_root(workspace, project)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*.md")}

    lint_workspace(conn, workspace, [project])

    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*.md")}
    assert after == before


def test_severity_filter_is_minimum_not_exact(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(workspace, project, "canvases/source.md", "[missing](missing.md)\n")
    _write(
        workspace,
        project,
        "archive/canvases/old.md",
        "# Old\n",
        status="archived",
    )
    _write(workspace, project, "canvases/archive-link.md", "[old](old.md)\n")
    report = lint_workspace(conn, workspace, [project])

    assert {finding.severity for finding in report.at_or_above("warning").findings} == {
        "error",
        "warning",
    }
    assert report.at_or_above("error").summary.errors == 1


def test_cli_json_is_structured_and_errors_exit_nonzero(
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(workspace, project, "canvases/source.md", "[missing](missing.md)\n")

    result = runner.invoke(
        app,
        ["lint", "--project", project, "--json", "--workspace", str(workspace)],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["findings"][0]["check_id"] == "broken_internal_link"
    assert payload["summary"]["errors"] == 1


def test_cli_warning_only_exits_zero(
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    _write(workspace, project, "canvases/source.md", "[old](old.md)\n")
    _write(
        workspace,
        project,
        "archive/canvases/old.md",
        "# Old\n",
        status="archived",
    )

    result = runner.invoke(app, ["lint", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "ARCHIVED_TARGET" in result.output.upper()


def test_cli_newer_format_refuses_before_registry_or_database_access(
    tmp_path: Path,
) -> None:
    workspace = WorkspaceRoot(tmp_path / "newer")
    write_format_marker(workspace, SUPPORTED_FORMAT + 1)
    (workspace / "system" / "projects.yml").write_text("not: a registry\n", encoding="utf-8")

    result = runner.invoke(app, ["lint", "--workspace", str(workspace), "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["findings"][0]["check_id"] == "workspace_format"
    assert not (workspace / ".ferumind" / "ferumind.sqlite").exists()
