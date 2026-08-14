"""IDX-01: ``ferumind verify-index`` detection and ``--fix`` repair bounds."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from ferumind.cli.main import app
from ferumind.core.frontmatter import generate_frontmatter
from ferumind.core.indexer import index_project
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.verify_index import (
    FindingKind,
    VerifyIndexReport,
    verify_and_maybe_repair,
    verify_index,
)
from tests.conftest import TEST_DESCRIPTION

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AGENTS_MD = REPO_ROOT / "AGENTS.md"


def _write_doc(workspace: WorkspaceRoot, project: str, rel: str, title: str) -> Path:
    path = workspace / "projects" / project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = generate_frontmatter(
        description=TEST_DESCRIPTION,
        doc_id=f"doc_{title.lower()}",
        project_key=project,
        title=title,
    )
    path.write_text(fm + f"# {title}\n\ncontent for {title}\n", encoding="utf-8")
    return path


def _kinds(report: VerifyIndexReport) -> set[FindingKind]:
    return {finding.kind for finding in report.findings}


def _durable_counts(conn: sqlite3.Connection) -> tuple[int, int, int]:
    return (
        int(conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]),
        int(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]),
        int(conn.execute("SELECT COUNT(*) FROM mcp_call_observations").fetchone()[0]),
    )


def _seed_observation(conn: sqlite3.Connection, project: str) -> None:
    conn.execute(
        """INSERT INTO mcp_call_observations
           (id, correlation_id, tool_name, project_key, created_at,
            ok, server_boot_id, process_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "obs_verify_index_seed",
            "corr_verify_index_seed",
            "get_context",
            project,
            "2026-01-01T00:00:00+00:00",
            1,
            "boot_verify_index",
            1,
        ),
    )
    conn.commit()


def _indexed_path(conn: sqlite3.Connection, project: str) -> str:
    row = conn.execute(
        "SELECT path FROM documents WHERE project_key = ? ORDER BY path LIMIT 1",
        (project,),
    ).fetchone()
    assert row is not None
    return str(row["path"])


def _insert_section_clone(
    conn: sqlite3.Connection,
    *,
    project: str,
    path: str,
    section_id: str | None = None,
) -> None:
    row = conn.execute(
        """SELECT title, heading, body, project_key, path, section_id, kind,
                  heading_text, heading_path_json, level, start_line, end_line,
                  content_sha256, size_bytes
           FROM section_index
           WHERE project_key = ? AND path = ?
           LIMIT 1""",
        (project, path),
    ).fetchone()
    assert row is not None
    values = list(row)
    if section_id is not None:
        values[5] = section_id
    conn.execute(
        """INSERT INTO section_index (
               title, heading, body, project_key, path, section_id, kind,
               heading_text, heading_path_json, level, start_line, end_line,
               content_sha256, size_bytes
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        values,
    )
    conn.commit()


def test_agents_md_carries_occasional_cadence_guidance() -> None:
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "ferumind verify-index" in text
    assert "from time to time" in text


def test_healthy_workspace_is_clean_and_cli_exits_zero(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    report = verify_index(conn, workspace, [project])
    assert report.ok
    assert report.findings == []
    assert report.documents_checked >= 2

    result = runner.invoke(app, ["verify-index", "--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    assert "Index clean" in result.output


def test_missing_on_disk_then_fix(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    path = _write_doc(workspace, project, "canvases/gone.md", "Gone")
    index_project(conn, workspace, project)
    path.unlink()

    broken = verify_index(conn, workspace, [project])
    assert "missing_on_disk" in _kinds(broken)

    fixed = verify_and_maybe_repair(conn, workspace, [project], fix=True)
    assert "missing_on_disk" not in _kinds(fixed)
    assert project in fixed.repaired_projects
    assert fixed.ok


def test_missing_in_index_then_fix(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _write_doc(workspace, project, "canvases/unindexed.md", "Unindexed")

    broken = verify_index(conn, workspace, [project])
    assert "missing_in_index" in _kinds(broken)

    fixed = verify_and_maybe_repair(conn, workspace, [project], fix=True)
    assert "missing_in_index" not in _kinds(fixed)
    assert project in fixed.repaired_projects


def test_hash_mismatch_then_fix(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rel = _indexed_path(conn, project)
    conn.execute(
        "UPDATE documents SET sha256 = ? WHERE project_key = ? AND path = ?",
        ("0" * 64, project, rel),
    )
    conn.commit()

    broken = verify_index(conn, workspace, [project])
    assert "hash_mismatch" in _kinds(broken)

    fixed = verify_and_maybe_repair(conn, workspace, [project], fix=True)
    assert "hash_mismatch" not in _kinds(fixed)
    assert project in fixed.repaired_projects


def test_invalid_utf8_document_is_a_finding_not_a_crash(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    rel = _indexed_path(conn, project)
    (workspace / "projects" / project / rel).write_bytes(b"\xff\xfe")

    report = verify_index(conn, workspace, [project])

    assert "hash_mismatch" in _kinds(report)
    assert any("UnicodeDecodeError" in finding.message for finding in report.findings)


def test_unterminated_frontmatter_is_a_finding_not_a_crash(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    rel = _indexed_path(conn, project)
    (workspace / "projects" / project / rel).write_text(
        "---\nid: unterminated\n",
        encoding="utf-8",
    )

    report = verify_index(conn, workspace, [project])

    assert {"hash_mismatch", "section_mismatch"} <= _kinds(report)


def test_section_mismatch_then_fix(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rel = _indexed_path(conn, project)
    conn.execute(
        "DELETE FROM section_index WHERE project_key = ? AND path = ?",
        (project, rel),
    )
    conn.commit()

    broken = verify_index(conn, workspace, [project])
    assert "section_mismatch" in _kinds(broken)

    fixed = verify_and_maybe_repair(conn, workspace, [project], fix=True)
    assert "section_mismatch" not in _kinds(fixed)
    assert project in fixed.repaired_projects


def test_orphan_section_then_fix(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    conn.execute(
        """INSERT INTO section_index (
               title, heading, body, project_key, path, section_id, kind,
               heading_text, heading_path_json, level, start_line, end_line,
               content_sha256, size_bytes
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "Orphan",
            "",
            "orphan body",
            project,
            "canvases/orphan-section.md",
            "orphan-sec",
            "preamble",
            None,
            "[]",
            None,
            1,
            1,
            "a" * 64,
            11,
        ),
    )
    conn.commit()

    broken = verify_index(conn, workspace, [project])
    assert "orphan_section" in _kinds(broken)

    fixed = verify_and_maybe_repair(conn, workspace, [project], fix=True)
    assert "orphan_section" not in _kinds(fixed)
    assert project in fixed.repaired_projects


def test_duplicate_section_then_fix(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rel = _indexed_path(conn, project)
    _insert_section_clone(conn, project=project, path=rel)

    broken = verify_index(conn, workspace, [project])
    assert "duplicate_section" in _kinds(broken)

    fixed = verify_and_maybe_repair(conn, workspace, [project], fix=True)
    assert "duplicate_section" not in _kinds(fixed)
    assert project in fixed.repaired_projects


def test_search_index_count_zero_then_fix(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rel = _indexed_path(conn, project)
    conn.execute(
        "DELETE FROM search_index WHERE project_key = ? AND path = ?",
        (project, rel),
    )
    conn.commit()

    broken = verify_index(conn, workspace, [project])
    assert "search_index_count" in _kinds(broken)
    assert any(
        finding.path == rel and "0 row" in finding.message
        for finding in broken.findings
        if finding.kind == "search_index_count"
    )

    fixed = verify_and_maybe_repair(conn, workspace, [project], fix=True)
    assert "search_index_count" not in _kinds(fixed)
    assert project in fixed.repaired_projects


def test_search_index_count_two_then_fix(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    rel = _indexed_path(conn, project)
    conn.execute(
        "INSERT INTO search_index (title, body, project_key, path) VALUES (?, ?, ?, ?)",
        ("dup", "dup body", project, rel),
    )
    conn.commit()

    broken = verify_index(conn, workspace, [project])
    assert "search_index_count" in _kinds(broken)
    assert any(
        finding.path == rel and "2 row" in finding.message
        for finding in broken.findings
        if finding.kind == "search_index_count"
    )

    fixed = verify_and_maybe_repair(conn, workspace, [project], fix=True)
    assert "search_index_count" not in _kinds(fixed)
    assert project in fixed.repaired_projects


def test_orphan_search_then_fix(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    conn.execute(
        "INSERT INTO search_index (title, body, project_key, path) VALUES (?, ?, ?, ?)",
        ("Ghost", "ghost body", project, "canvases/orphan-search.md"),
    )
    conn.commit()

    broken = verify_index(conn, workspace, [project])
    assert "orphan_search" in _kinds(broken)

    fixed = verify_and_maybe_repair(conn, workspace, [project], fix=True)
    assert "orphan_search" not in _kinds(fixed)
    assert project in fixed.repaired_projects


def test_dangling_snapshot_survives_fix(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    missing_dir = workspace / ".ferumind" / "snapshots" / "missing-snap-dir"
    assert not missing_dir.exists()
    conn.execute(
        "INSERT INTO snapshots (id, project_key, snapshot_dir, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            "snap_dangling_verify",
            project,
            str(missing_dir),
            "test",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    # Also poison a repairable finding so --fix actually runs rebuild.
    rel = _indexed_path(conn, project)
    conn.execute(
        "UPDATE documents SET sha256 = ? WHERE project_key = ? AND path = ?",
        ("f" * 64, project, rel),
    )
    conn.commit()

    before = _durable_counts(conn)
    broken = verify_index(conn, workspace, [project])
    assert "dangling_snapshot" in _kinds(broken)
    assert "hash_mismatch" in _kinds(broken)

    fixed = verify_and_maybe_repair(conn, workspace, [project], fix=True)
    assert "hash_mismatch" not in _kinds(fixed)
    assert "dangling_snapshot" in _kinds(fixed)
    assert project in fixed.repaired_projects
    assert _durable_counts(conn) == before
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE id = ?",
            ("snap_dangling_verify",),
        ).fetchone()[0]
        == 1
    )


def test_default_mode_creates_no_writes(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _seed_observation(conn, project)
    db_path = workspace / ".ferumind" / "ferumind.sqlite"
    before_counts = _durable_counts(conn)
    before_mtime = db_path.stat().st_mtime_ns

    report = verify_index(conn, workspace, [project])
    assert report.ok
    assert _durable_counts(conn) == before_counts

    result = runner.invoke(app, ["verify-index", "--workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    assert _durable_counts(conn) == before_counts
    assert db_path.stat().st_mtime_ns == before_mtime


def test_fix_leaves_durable_history_unchanged(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _seed_observation(conn, project)
    rel = _indexed_path(conn, project)
    conn.execute(
        "DELETE FROM section_index WHERE project_key = ? AND path = ?",
        (project, rel),
    )
    conn.commit()
    before = _durable_counts(conn)
    assert before[0] > 0
    assert before[1] > 0
    assert before[2] > 0

    broken = verify_index(conn, workspace, [project])
    assert "section_mismatch" in _kinds(broken)

    fixed = verify_and_maybe_repair(conn, workspace, [project], fix=True)
    assert "section_mismatch" not in _kinds(fixed)
    assert project in fixed.repaired_projects
    assert _durable_counts(conn) == before

    result = runner.invoke(
        app, ["verify-index", "--fix", "--project", project, "--workspace", str(workspace)]
    )
    # Already clean after the core repair; CLI --fix is a no-op rebuild-wise.
    assert result.exit_code == 0, result.output
    assert _durable_counts(conn) == before
