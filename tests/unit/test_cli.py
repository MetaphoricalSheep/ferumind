"""CLI command tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from lattice.cli.main import app
from lattice.core import compacts
from lattice.core.paths import WorkspaceRoot

runner = CliRunner()


def _handoff_body(prompt: str) -> str:
    return f"## Handoff Prompt\n\n{prompt}\n\n## Short TL;DR\n\nOriginal.\n"


def test_compact_reseal_command(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    draft = compacts.create_compact_draft(conn, workspace)
    prompt = "Use this compact."
    compacts.finalize_compact(
        conn,
        workspace,
        token=draft.token,
        handoff_prompt=prompt,
        final_markdown=_handoff_body(prompt),
    )
    path = Path(workspace) / compacts.compact_relative_path(draft.token)
    path.write_text(
        path.read_text(encoding="utf-8").replace("Original.", "Edited."),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["compact", "reseal", draft.token, "--workspace", str(workspace)],
    )

    assert result.exit_code == 0, result.output
    assert f"Resealed compact {draft.token}" in result.output
    read = compacts.read_compact(workspace, token=draft.token)
    assert read.frontmatter.state == "finalized"
    assert read.integrity_ok is True


def test_project_list_command(workspace: WorkspaceRoot, project: str) -> None:
    result = runner.invoke(app, ["project", "list", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "demo (Demo) — registry, folder, database" in result.output


def test_project_list_command_empty_workspace(workspace: WorkspaceRoot) -> None:
    result = runner.invoke(app, ["project", "list", "--workspace", str(workspace)])

    assert result.exit_code == 0, result.output
    assert "No projects found." in result.output


def test_project_delete_command_cleans_stale_state(workspace: WorkspaceRoot, project: str) -> None:
    project_folder = Path(workspace) / "projects" / project
    removed_folder = Path(workspace) / "removed-demo"
    project_folder.rename(removed_folder)

    result = runner.invoke(
        app,
        ["project", "delete", project, "--yes", "--workspace", str(workspace)],
    )

    assert result.exit_code == 0, result.output
    assert "Cleaned stale state for project 'demo'" in result.output
    assert not project_folder.exists()
    assert removed_folder.exists()

    list_result = runner.invoke(app, ["project", "list", "--workspace", str(workspace)])
    assert "No projects found." in list_result.output


def test_project_delete_command_prompts_without_yes(workspace: WorkspaceRoot, project: str) -> None:
    result = runner.invoke(
        app,
        ["project", "delete", project, "--workspace", str(workspace)],
        input="n\n",
    )

    assert result.exit_code == 1
    assert "Aborted." in result.output
    assert (Path(workspace) / "projects" / project).exists()


def test_project_delete_command_unknown_key(workspace: WorkspaceRoot) -> None:
    result = runner.invoke(
        app,
        ["project", "delete", "nope", "--yes", "--workspace", str(workspace)],
    )

    assert result.exit_code == 1
    assert "Cannot delete project" in result.output
