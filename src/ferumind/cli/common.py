"""Shared CLI path resolution without command-specific side effects."""

from __future__ import annotations

from pathlib import Path

import typer

WORKSPACE_OPTION = typer.Option(
    "--workspace",
    help="Workspace directory (default: FERUMIND_WORKSPACE or ./workspace)",
)


def workspace_root(workspace: Path | None) -> Path:
    from ferumind.core.config import load_config
    from ferumind.core.paths import resolve_repo_root, resolve_workspace_root

    config = load_config(workspace)
    if config.workspace_path.is_absolute():
        return config.workspace_path.resolve()
    repo = resolve_repo_root()
    return resolve_workspace_root(repo, str(config.workspace_path))


def database_path(workspace: Path) -> Path:
    from ferumind.core.paths import contained_path

    return contained_path(workspace, ".ferumind/ferumind.sqlite")
