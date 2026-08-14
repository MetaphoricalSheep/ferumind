"""The two places Ferumind still reaches past the MCP SDK's public API.

Everything else the server needs is public on mcp 2.x: ``version`` and
``middleware`` are constructor parameters, ``add_request_handler`` replaces the
removed low-level decorators, and ``ToolManager`` exposes ``list_tools`` and
``get_tool``. What remains has no public equivalent:

* **the low-level server** — ``MCPServer.run_stdio_async()`` owns its own
  transport, so driving the bounded, redacting stdio streams in
  :mod:`ferumind.mcp.server` and registering the per-file-MIME
  ``resources/read`` handler both need the ``Server`` underneath.
* **the tool manager** — needed to reach its *public* accessors, and to replace
  a registered tool's generated argument metadata, which nothing public exposes.

Both accessors **fail closed**. A silent ``getattr`` miss would mean serving
with no argument validation and no exception sanitisation — an SDK rename must
stop startup, not quietly downgrade it. The supported range is capped in
``pyproject.toml`` precisely because these two attachment points exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.lowlevel.server import Server
    from mcp.server.mcpserver.tools.base import Tool

_RANGE_HINT = (
    "The supported MCP SDK range is capped in pyproject.toml; a version "
    "outside it may have renamed this attachment point."
)


def lowlevel_server(mcp: object) -> Server[Any]:
    """Return the ``Server`` that ``MCPServer`` wraps.

    Renamed from ``_mcp_server`` to ``_lowlevel_server`` in mcp 2.0.
    """
    server = getattr(mcp, "_lowlevel_server", None)
    if server is None:
        raise RuntimeError(
            "The MCP low-level server is unavailable, so Ferumind cannot "
            "install its bounded stdio transport or its resources/read "
            f"handler. Refusing to serve. {_RANGE_HINT}"
        )
    return cast("Server[Any]", server)


def registered_tools(mcp: object) -> list[Tool]:
    """Return every registered tool, through the manager's public accessor.

    ``ToolManager.list_tools()`` is public on mcp 2.x; only reaching the
    manager itself is not.
    """
    manager = getattr(mcp, "_tool_manager", None)
    if manager is None:
        raise RuntimeError(
            "The MCP tool manager is unavailable, so Ferumind cannot install "
            "its tool boundary. Refusing to serve without argument validation "
            f"and error sanitisation. {_RANGE_HINT}"
        )
    lister = getattr(manager, "list_tools", None)
    if lister is None:
        raise RuntimeError(
            "The MCP tool manager exposes no list_tools(), so Ferumind cannot "
            f"install its tool boundary. Refusing to serve. {_RANGE_HINT}"
        )
    return cast("list[Tool]", lister())
