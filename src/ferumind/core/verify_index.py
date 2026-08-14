"""Verify (and optionally repair) derived index state against Markdown on disk.

Reconcile's hot path only compares ``(mtime_ns, size_bytes)``. This module
catches the cases that check cannot see: content rewritten at the same size
and mtime, missing or wrong section rows, orphan / duplicate FTS rows, and
dangling snapshot metadata. Default mode is read-only; ``--fix`` delegates
to :func:`ferumind.core.indexer.rebuild_index` and never writes Markdown or
durable history tables.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import Field

from ferumind.core.document_map import (
    DocumentSection,
    derive_sections,
    frontmatter_line_range,
    split_document_lines,
)
from ferumind.core.documents import compute_sha256
from ferumind.core.errors import FrontmatterInvalidError
from ferumind.core.format import SUPPORTED_FORMAT, read_format
from ferumind.core.indexer import project_dir_for, rebuild_index
from ferumind.core.paths import PathSafetyError, WorkspaceRoot, contained_path, is_under_root
from ferumind.core.types import DbConnection, DbRow, StrictModel
from ferumind.db.database import discover_migrations

FindingKind = Literal[
    "missing_on_disk",
    "missing_in_index",
    "hash_mismatch",
    "section_mismatch",
    "orphan_section",
    "duplicate_section",
    "search_index_count",
    "orphan_search",
    "integrity_check",
    "schema_version",
    "workspace_format",
    "dangling_snapshot",
]

#: Findings ``rebuild_index`` can clear. Schema / format / integrity / dangling
#: snapshot metadata are reported only — ``--fix`` must not invent history.
_REPAIRABLE: frozenset[FindingKind] = frozenset(
    {
        "missing_on_disk",
        "missing_in_index",
        "hash_mismatch",
        "section_mismatch",
        "orphan_section",
        "duplicate_section",
        "search_index_count",
        "orphan_search",
    }
)


class IndexFinding(StrictModel):
    """One divergence between derived state and Markdown (or schema) truth."""

    kind: FindingKind
    message: str
    project_key: str | None = None
    path: str | None = None


class VerifyIndexReport(StrictModel):
    """Outcome of one ``verify-index`` pass."""

    findings: list[IndexFinding]
    projects_checked: list[str]
    documents_checked: int
    repaired_projects: list[str] = Field(default_factory=list)
    repair_documents_indexed: int = 0
    repair_errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def repairable_projects(self) -> list[str]:
        keys = {
            finding.project_key
            for finding in self.findings
            if finding.kind in _REPAIRABLE and finding.project_key is not None
        }
        return sorted(keys)


def verify_index(
    conn: DbConnection,
    workspace: WorkspaceRoot,
    project_keys: Sequence[str],
    *,
    include_workspace_checks: bool = True,
) -> VerifyIndexReport:
    """Run IDX-01 checks without writing.

    ``include_workspace_checks=False`` is the narrow per-project seam used by
    lint while it holds one project lock.  It avoids repeating SQLite
    integrity/schema and snapshot checks for every locked project; normal
    ``verify-index`` calls retain the complete default.
    """
    findings: list[IndexFinding] = []
    if include_workspace_checks:
        findings.extend(_check_schema_and_format(conn, workspace))
        findings.extend(_check_snapshots(conn, workspace, project_keys))

    documents_checked = 0
    for project_key in project_keys:
        project_findings, docs = _check_project(conn, workspace, project_key)
        findings.extend(project_findings)
        documents_checked += docs

    return VerifyIndexReport(
        findings=findings,
        projects_checked=list(project_keys),
        documents_checked=documents_checked,
    )


def verify_and_maybe_repair(
    conn: DbConnection,
    workspace: WorkspaceRoot,
    project_keys: Sequence[str],
    *,
    fix: bool,
) -> VerifyIndexReport:
    """Verify; if *fix* and repairable findings exist, rebuild those projects."""
    report = verify_index(conn, workspace, project_keys)
    if not fix or not report.repairable_projects:
        return report

    repair_keys = [key for key in project_keys if key in set(report.repairable_projects)]
    if not repair_keys:
        return report

    repair_result = rebuild_index(conn, workspace, repair_keys)
    rechecked = verify_index(conn, workspace, project_keys)
    return VerifyIndexReport(
        findings=rechecked.findings,
        projects_checked=rechecked.projects_checked,
        documents_checked=rechecked.documents_checked,
        repaired_projects=repair_keys,
        repair_documents_indexed=repair_result.documents_indexed,
        repair_errors=list(repair_result.error_messages),
    )


def _check_schema_and_format(conn: DbConnection, workspace: WorkspaceRoot) -> list[IndexFinding]:
    findings: list[IndexFinding] = []
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    integrity_text = str(integrity[0]) if integrity is not None else "missing"
    if integrity_text != "ok":
        findings.append(
            IndexFinding(
                kind="integrity_check",
                message=f"PRAGMA integrity_check reported {integrity_text!r}",
            )
        )

    migrations = discover_migrations()
    expected_schema = migrations[-1].number if migrations else 0
    row = conn.execute("PRAGMA user_version").fetchone()
    user_version = int(row[0]) if row is not None else -1
    if user_version != expected_schema:
        findings.append(
            IndexFinding(
                kind="schema_version",
                message=(
                    f"PRAGMA user_version is {user_version}; this build expects {expected_schema}"
                ),
            )
        )

    found_format = read_format(workspace)
    if found_format != SUPPORTED_FORMAT:
        findings.append(
            IndexFinding(
                kind="workspace_format",
                message=(
                    f"workspace format is {found_format!r}; this build expects {SUPPORTED_FORMAT}"
                ),
            )
        )
    return findings


def _check_snapshots(
    conn: DbConnection,
    workspace: WorkspaceRoot,
    project_keys: Sequence[str],
) -> list[IndexFinding]:
    findings: list[IndexFinding] = []
    key_set = set(project_keys)
    rows = conn.execute("SELECT id, project_key, snapshot_dir FROM snapshots").fetchall()
    for row in rows:
        project_key = str(row["project_key"])
        if project_key not in key_set:
            continue
        snapshot_dir = Path(str(row["snapshot_dir"]))
        if not is_under_root(snapshot_dir, workspace):
            findings.append(
                IndexFinding(
                    kind="dangling_snapshot",
                    project_key=project_key,
                    path=str(row["id"]),
                    message=(
                        f"snapshot {row['id']} path escapes the workspace: {row['snapshot_dir']}"
                    ),
                )
            )
            continue
        if not snapshot_dir.is_dir():
            findings.append(
                IndexFinding(
                    kind="dangling_snapshot",
                    project_key=project_key,
                    path=str(row["id"]),
                    message=(f"snapshot {row['id']} directory missing: {row['snapshot_dir']}"),
                )
            )
    return findings


def _check_project(
    conn: DbConnection,
    workspace: WorkspaceRoot,
    project_key: str,
) -> tuple[list[IndexFinding], int]:
    findings: list[IndexFinding] = []
    project_dir = project_dir_for(workspace, project_key)
    on_disk: set[str] = _markdown_paths(project_dir) if project_dir.is_dir() else set()

    indexed_rows = conn.execute(
        "SELECT path, sha256 FROM documents WHERE project_key = ?",
        (project_key,),
    ).fetchall()
    indexed: dict[str, str] = {str(row["path"]): str(row["sha256"]) for row in indexed_rows}

    for path in sorted(on_disk - indexed.keys()):
        findings.append(
            IndexFinding(
                kind="missing_in_index",
                project_key=project_key,
                path=path,
                message=f"{path} is on disk but not in documents",
            )
        )

    for path in sorted(indexed.keys() - on_disk):
        findings.append(
            IndexFinding(
                kind="missing_on_disk",
                project_key=project_key,
                path=path,
                message=f"{path} is indexed but missing on disk",
            )
        )

    documents_checked = 0
    for path in sorted(on_disk & indexed.keys()):
        documents_checked += 1
        file_path = contained_path(project_dir, path)
        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            findings.append(
                IndexFinding(
                    kind="hash_mismatch",
                    project_key=project_key,
                    path=path,
                    message=f"{path}: cannot read ({type(exc).__name__})",
                )
            )
            continue

        digest = compute_sha256(content)
        if digest != indexed[path]:
            findings.append(
                IndexFinding(
                    kind="hash_mismatch",
                    project_key=project_key,
                    path=path,
                    message=f"{path}: documents.sha256 does not match file contents",
                )
            )

        try:
            findings.extend(_compare_sections(conn, project_key, path, content))
        except FrontmatterInvalidError as exc:
            findings.append(
                IndexFinding(
                    kind="section_mismatch",
                    project_key=project_key,
                    path=path,
                    message=f"{path}: cannot derive sections ({type(exc).__name__})",
                )
            )
        findings.extend(_check_search_row_count(conn, project_key, path))

    findings.extend(_orphan_sections(conn, project_key))
    findings.extend(_duplicate_sections(conn, project_key))
    findings.extend(_orphan_search_rows(conn, project_key))
    return findings, documents_checked


def _markdown_paths(project_dir: Path) -> set[str]:
    paths: set[str] = set()
    for md_file in project_dir.rglob("*.md"):
        rel = md_file.relative_to(project_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        try:
            contained_path(project_dir, rel.as_posix())
        except PathSafetyError:
            continue
        if not md_file.is_file():
            continue
        paths.add(rel.as_posix())
    return paths


def _compare_sections(
    conn: DbConnection, project_key: str, path: str, content: str
) -> list[IndexFinding]:
    lines = split_document_lines(content)
    _fm, body_start = frontmatter_line_range(content)
    expected = derive_sections(lines, body_start, len(lines))
    indexed = conn.execute(
        """SELECT section_id, kind, heading_text, heading_path_json, level,
                  start_line, end_line, content_sha256, size_bytes, body
           FROM section_index
           WHERE project_key = ? AND path = ?
           ORDER BY CAST(start_line AS INTEGER), section_id""",
        (project_key, path),
    ).fetchall()

    if len(indexed) != len(expected):
        return [
            IndexFinding(
                kind="section_mismatch",
                project_key=project_key,
                path=path,
                message=(
                    f"{path}: section_index has {len(indexed)} row(s); "
                    f"derive_sections produced {len(expected)}"
                ),
            )
        ]

    findings: list[IndexFinding] = []
    for row, section in zip(indexed, expected, strict=True):
        mismatches = _section_field_mismatches(row, section, lines)
        if mismatches:
            findings.append(
                IndexFinding(
                    kind="section_mismatch",
                    project_key=project_key,
                    path=path,
                    message=(
                        f"{path} section {section.section_id}: "
                        f"fields disagree ({', '.join(mismatches)})"
                    ),
                )
            )
    return findings


def _section_field_mismatches(row: DbRow, section: DocumentSection, lines: list[str]) -> list[str]:
    """Return indexed field names that disagree with *section*."""
    level = None if row["level"] in (None, "") else int(row["level"])
    heading_text = row["heading_text"] or None
    heading_path = json.loads(str(row["heading_path_json"]))
    body = "\n".join(lines[section.start_line - 1 : section.end_line])
    checks: list[tuple[str, object, object]] = [
        ("section_id", row["section_id"], section.section_id),
        ("kind", row["kind"], section.kind),
        ("heading_text", heading_text, section.heading_text),
        ("heading_path", heading_path, section.heading_path),
        ("level", level, section.level),
        ("start_line", int(row["start_line"]), section.start_line),
        ("end_line", int(row["end_line"]), section.end_line),
        ("content_sha256", row["content_sha256"], section.content_sha256),
        ("size_bytes", int(row["size_bytes"]), section.size_bytes),
        ("body", row["body"], body),
    ]
    return [name for name, actual, expected in checks if actual != expected]


def _check_search_row_count(conn: DbConnection, project_key: str, path: str) -> list[IndexFinding]:
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM search_index
           WHERE project_key = ? AND path = ?""",
        (project_key, path),
    ).fetchone()
    count = int(row["n"]) if row is not None else 0
    if count == 1:
        return []
    return [
        IndexFinding(
            kind="search_index_count",
            project_key=project_key,
            path=path,
            message=f"{path}: search_index has {count} row(s); expected exactly 1",
        )
    ]


def _orphan_sections(conn: DbConnection, project_key: str) -> list[IndexFinding]:
    rows = conn.execute(
        """SELECT si.path, si.section_id
           FROM section_index si
           LEFT JOIN documents d
             ON d.project_key = si.project_key AND d.path = si.path
           WHERE si.project_key = ? AND d.path IS NULL""",
        (project_key,),
    ).fetchall()
    return [
        IndexFinding(
            kind="orphan_section",
            project_key=project_key,
            path=str(row["path"]),
            message=(
                f"{row['path']} section {row['section_id']}: "
                "section_index row has no documents parent"
            ),
        )
        for row in rows
    ]


def _duplicate_sections(conn: DbConnection, project_key: str) -> list[IndexFinding]:
    rows = conn.execute(
        """SELECT path, section_id, COUNT(*) AS n
           FROM section_index
           WHERE project_key = ?
           GROUP BY path, section_id
           HAVING n > 1""",
        (project_key,),
    ).fetchall()
    return [
        IndexFinding(
            kind="duplicate_section",
            project_key=project_key,
            path=str(row["path"]),
            message=(
                f"{row['path']} section {row['section_id']}: "
                f"{row['n']} duplicate section_index row(s)"
            ),
        )
        for row in rows
    ]


def _orphan_search_rows(conn: DbConnection, project_key: str) -> list[IndexFinding]:
    rows = conn.execute(
        """SELECT si.path
           FROM search_index si
           LEFT JOIN documents d
             ON d.project_key = si.project_key AND d.path = si.path
           WHERE si.project_key = ? AND d.path IS NULL""",
        (project_key,),
    ).fetchall()
    return [
        IndexFinding(
            kind="orphan_search",
            project_key=project_key,
            path=str(row["path"]),
            message=f"{row['path']}: search_index row has no documents parent",
        )
        for row in rows
    ]
