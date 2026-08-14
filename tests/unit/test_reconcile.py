"""Tests for reconcile-on-read (00 D12): out-of-band edits are first-class."""

from __future__ import annotations

import os
import sqlite3

from ferumind.core.documents import compute_sha256
from ferumind.core.indexer import get_indexed_signature, index_project
from ferumind.core.operations import get_operation, list_operations
from ferumind.core.patch_writes import propose_exact_replace_patch
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.reconcile import (
    reconcile_document,
    reconcile_project,
)


def _hand_edit(workspace: WorkspaceRoot, project: str, rel: str, extra: str) -> None:
    path = workspace / "projects" / project / rel
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000))


def test_clean_document_reconciles_as_noop(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    index_project(conn, workspace, project)
    outcome = reconcile_document(conn, workspace, project, "spine.md")
    assert not outcome.drifted
    assert not any(op.source == "out-of-band" for op in list_operations(conn, project, limit=100))


def test_drift_reindexes_and_logs_out_of_band(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    index_project(conn, workspace, project)
    _hand_edit(workspace, project, "spine.md", "\nedited by hand\n")
    outcome = reconcile_document(conn, workspace, project, "spine.md")
    assert outcome.drifted

    signature = get_indexed_signature(conn, project, "spine.md")
    assert signature is not None
    on_disk = (workspace / "projects" / project / "spine.md").read_text(encoding="utf-8")
    assert signature[2] == compute_sha256(on_disk)

    oob = [op for op in list_operations(conn, project, limit=10) if op.source == "out-of-band"]
    assert oob
    assert oob[0].operation_type == "out_of_band_edit"
    assert oob[0].target_path == "spine.md"


def test_drift_marks_pending_proposals_stale(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    index_project(conn, workspace, project)
    proposal = propose_exact_replace_patch(
        conn,
        workspace,
        project,
        path="spine.md",
        old_string="# Demo",
        new_string="# Demo (renamed)",
    )
    _hand_edit(workspace, project, "spine.md", "\nedited by hand\n")
    outcome = reconcile_document(conn, workspace, project, "spine.md")
    assert outcome.drifted
    assert outcome.proposals_staled == 1
    op = get_operation(conn, proposal.operation_id)
    assert op is not None
    assert op.state == "stale"


def test_out_of_band_delete_prunes_index_and_stales(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    index_project(conn, workspace, project)
    (workspace / "projects" / project / "rules" / "00-project.md").unlink()
    outcome = reconcile_document(conn, workspace, project, "rules/00-project.md")
    assert outcome.drifted
    assert outcome.removed
    assert get_indexed_signature(conn, project, "rules/00-project.md") is None
    deletes = [
        op
        for op in list_operations(conn, project, limit=10)
        if op.operation_type == "out_of_band_delete"
    ]
    assert deletes


def test_touch_without_content_change_is_silent(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    index_project(conn, workspace, project)
    path = workspace / "projects" / project / "spine.md"
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000))
    outcome = reconcile_document(conn, workspace, project, "spine.md")
    assert not outcome.drifted
    assert not any(op.source == "out-of-band" for op in list_operations(conn, project, limit=100))


def test_reconcile_project_catches_new_files(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    index_project(conn, workspace, project)
    new_file = workspace / "projects" / project / "memory" / "note.md"
    new_file.parent.mkdir(parents=True, exist_ok=True)
    new_file.write_text("# Hand-made note\n", encoding="utf-8")
    drifted = reconcile_project(conn, workspace, project)
    assert drifted == 1
    assert get_indexed_signature(conn, project, "memory/note.md") is not None
