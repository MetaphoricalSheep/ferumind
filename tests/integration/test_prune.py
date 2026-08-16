"""End-to-end retention: what an operator's prune looks like from outside.

The unit suite proves prune removes the right rows and directories. This one
answers the two questions that only the whole stack can: does a reclaimed
snapshot still produce a sensible error through the MCP surface, and is the
workspace still consistent afterwards by Ferumind's own check.

Calls go through the registered tool surface, using ``test_mcp_surface``'s
``call``/``ok`` helpers so a result is checked against its declared
``outputSchema`` exactly as it is there.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ferumind.core.paths import WorkspaceRoot, contained_path
from ferumind.core.retention import RetentionPolicy, prune_workspace
from ferumind.core.verify_index import verify_and_maybe_repair
from ferumind.db.database import Database
from ferumind.mcp.sdk_internals import registered_tools
from tests.integration.test_mcp_surface import ToolMap, call, ok

SNAPSHOT_STAMP_FORMAT = "%Y%m%dT%H%M%S"


@pytest.fixture
def tools(workspace: WorkspaceRoot) -> Iterator[ToolMap]:
    """The registered tool surface bound to this test's workspace."""
    from ferumind.mcp import server, tool_context

    tool_context.reset_tool_context()
    tool_context.init_tool_context(Path(workspace))
    server.register_all_tools()
    yield {tool.name: tool.fn for tool in registered_tools(server.mcp)}
    tool_context.reset_tool_context()


@pytest.fixture
def connection(workspace: WorkspaceRoot) -> Iterator[sqlite3.Connection]:
    database = Database(contained_path(workspace, ".ferumind/ferumind.sqlite"))
    database.init_schema()
    conn = database.get_connection()
    yield conn
    conn.close()


def _backdate_snapshots(conn: sqlite3.Connection, workspace: WorkspaceRoot, days: int) -> None:
    """Rename every snapshot directory to the stamp it would carry when aged.

    The directory name is what retention dates a snapshot by, so this is the
    whole of "make it old" — no clock injection and no waiting.
    """
    stamp = (datetime.now(UTC) - timedelta(days=days)).strftime(SNAPSHOT_STAMP_FORMAT)
    for row in conn.execute("SELECT id, snapshot_dir FROM snapshots").fetchall():
        directory = Path(str(row["snapshot_dir"]))
        if not directory.is_dir():
            continue
        aged = directory.with_name(f"{stamp}-{row['id']}")
        directory.rename(aged)
        conn.execute("UPDATE snapshots SET snapshot_dir = ? WHERE id = ?", (str(aged), row["id"]))
    conn.commit()


@pytest.fixture
def edited_document(tools: ToolMap) -> tuple[str, str]:
    """A project with one document that has been edited, so a snapshot exists."""
    ok(tools, "create_project", key="demo", title="Demo")
    created = ok(
        tools,
        "create_document",
        project="demo",
        folder_path="canvases",
        title="Plan",
        description="Fixture canvas used by the prune integration tests.",
        content="# Plan\n\nalpha beta gamma\n",
    )
    path = str(created["path"])
    proposal = ok(
        tools,
        "propose_exact_replace_patch",
        project="demo",
        path=path,
        old_string="alpha beta gamma",
        new_string="delta epsilon zeta",
    )
    ok(tools, "apply_patch", project="demo", operation_id=str(proposal["operation_id"]))
    return "demo", path


class TestReclaimedSnapshots:
    def test_a_reclaimed_snapshot_answers_snapshot_not_found(
        self,
        tools: ToolMap,
        workspace: WorkspaceRoot,
        connection: sqlite3.Connection,
        edited_document: tuple[str, str],
    ) -> None:
        project, path = edited_document
        snapshot_id = str(
            ok(tools, "list_snapshots", project=project, path=path)["snapshots"][0]["id"]
        )
        assert ok(tools, "read_snapshot", project=project, snapshot_id=snapshot_id)

        _backdate_snapshots(connection, workspace, days=400)
        prune_workspace(
            connection,
            workspace,
            policy=RetentionPolicy(keep_recent_snapshots=0),
            dry_run=False,
        )

        for tool in ("read_snapshot", "restore_snapshot"):
            envelope = call(tools, tool, project=project, snapshot_id=snapshot_id)
            assert envelope["ok"] is False, tool
            assert envelope["error_code"] == "SNAPSHOT_NOT_FOUND", tool

    def test_the_document_itself_is_untouched(
        self,
        tools: ToolMap,
        workspace: WorkspaceRoot,
        connection: sqlite3.Connection,
        edited_document: tuple[str, str],
    ) -> None:
        project, path = edited_document
        before = ok(tools, "read_document", project=project, path=path)

        _backdate_snapshots(connection, workspace, days=400)
        prune_workspace(
            connection,
            workspace,
            policy=RetentionPolicy(keep_recent_snapshots=0),
            dry_run=False,
        )

        after = ok(tools, "read_document", project=project, path=path)
        assert after["content"] == before["content"]
        assert after["document_sha256"] == before["document_sha256"]

    def test_the_index_is_clean_afterwards(
        self,
        workspace: WorkspaceRoot,
        connection: sqlite3.Connection,
        edited_document: tuple[str, str],
    ) -> None:
        project, _ = edited_document

        _backdate_snapshots(connection, workspace, days=400)
        prune_workspace(
            connection,
            workspace,
            policy=RetentionPolicy(keep_recent_snapshots=0),
            dry_run=False,
        )

        report = verify_and_maybe_repair(connection, workspace, [project], fix=False)
        assert report.ok, [finding.message for finding in report.findings]

    def test_the_operation_log_still_records_the_edit(
        self,
        tools: ToolMap,
        workspace: WorkspaceRoot,
        connection: sqlite3.Connection,
        edited_document: tuple[str, str],
    ) -> None:
        """The history survives losing the bytes it points at."""
        project, path = edited_document
        before = ok(tools, "operation_log", project=project, path=path)["operations"]

        _backdate_snapshots(connection, workspace, days=400)
        prune_workspace(
            connection,
            workspace,
            policy=RetentionPolicy(keep_recent_snapshots=0, diff_scrub_age_days=1),
            dry_run=False,
        )

        after = ok(tools, "operation_log", project=project, path=path)["operations"]
        assert len(after) == len(before)
        assert [entry["operation_type"] for entry in after] == [
            entry["operation_type"] for entry in before
        ]
