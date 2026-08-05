"""Shared MCP tool context: workspace, database, format gate, error mapping.

The server is stateless per call (00 D8): this module holds only process
configuration — workspace root, database handle, format gate, transport —
never conversation state. Every scoped tool resolves its project through
:func:`scoped_project`, which validates the ``project`` assertion against
the registry.
"""

from __future__ import annotations

import logging
from pathlib import Path

from mcp.types import CallToolResult

from ferumind.core.config import Config, load_config
from ferumind.core.errors import FerumindError
from ferumind.core.format import FormatGate
from ferumind.core.paths import (
    PathSafetyError,
    WorkspaceRoot,
    contained_path,
    resolve_repo_root,
    resolve_workspace_root,
)
from ferumind.core.registry import ProjectEntry, require_project
from ferumind.db.database import Database
from ferumind.mcp.models import make_error

_workspace_root: WorkspaceRoot | None = None
_database: Database | None = None
_config: Config | None = None
_format_gate: FormatGate | None = None
_transport: str = "stdio"
logger = logging.getLogger(__name__)


def init_tool_context(workspace_path: Path | None = None, *, transport: str = "stdio") -> None:
    """Initialize the shared tool context with workspace path and database."""
    global _workspace_root, _database, _config, _format_gate, _transport
    _config = load_config(workspace_path)
    if _config.workspace_path.is_absolute():
        _workspace_root = WorkspaceRoot(_config.workspace_path.resolve())
    else:
        repo = resolve_repo_root()
        _workspace_root = resolve_workspace_root(repo, str(_config.workspace_path))
    if not _workspace_root.is_dir():
        raise RuntimeError(
            "Ferumind workspace is not initialized; run scripts/bootstrap_workspace.py first"
        )
    db_path = contained_path(_workspace_root, f"{_config.ferumind_dir_name}/ferumind.sqlite")
    _database = Database(db_path)
    _database.init_schema()
    _format_gate = FormatGate(_workspace_root)
    _transport = transport


def reset_tool_context() -> None:
    """Clear the shared context (test isolation)."""
    global _workspace_root, _database, _config, _format_gate, _transport
    _workspace_root = None
    _database = None
    _config = None
    _format_gate = None
    _transport = "stdio"


def require_workspace() -> WorkspaceRoot:
    if _workspace_root is None:
        init_tool_context()
    if _workspace_root is None:  # defensive: initialization either sets it or raises
        raise RuntimeError("Ferumind workspace context failed to initialize")
    return _workspace_root


def require_database() -> Database:
    if _database is None:
        init_tool_context()
    if _database is None:
        raise RuntimeError("Ferumind database context failed to initialize")
    return _database


def require_config() -> Config:
    if _config is None:
        init_tool_context()
    if _config is None:
        raise RuntimeError("Ferumind configuration context failed to initialize")
    return _config


def require_format_gate() -> FormatGate:
    if _format_gate is None:
        init_tool_context()
    if _format_gate is None:
        raise RuntimeError("Ferumind format gate failed to initialize")
    return _format_gate


def current_transport() -> str:
    return _transport


def scoped_project(project: str | None) -> ProjectEntry:
    """Validate the ``project`` assertion against the registry."""
    return require_project(require_workspace(), project)


def error_result(exc: Exception, *, project: str | None = None) -> CallToolResult:
    """Map a core exception to a structured MCP error result."""
    if isinstance(exc, FerumindError):
        return make_error(exc.code, str(exc), exc.details, project=project)
    if isinstance(exc, PathSafetyError):
        return make_error(
            "WORKSPACE_MISMATCH",
            "Path is outside the configured workspace boundary",
            project=project,
        )
    logger.error("Unexpected tool error (type=%s)", type(exc).__name__)
    return make_error(
        "INTERNAL_ERROR",
        "Ferumind encountered an unexpected internal error",
        project=project,
    )
