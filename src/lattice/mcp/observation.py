"""Central MCP tool-call observation wrapper (spec-mcp §8).

Wraps every registered tool function to record metadata before and after
each call: tool name, correlation id, project, ok/error code, transport,
duration, result size, and argument keys — never argument values or
document content. ``get_context`` results additionally record the payload
telemetry (rules/spine bytes, documents count). Observation failures are
swallowed (logged) so telemetry can never break a user call.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from typing import Any, cast

from mcp.types import CallToolResult

from lattice.core.observations import new_correlation_id, record_mcp_call_observation
from lattice.core.types import JsonObject
from lattice.mcp.models import make_error
from lattice.mcp.tool_context import current_transport, require_database

logger = logging.getLogger(__name__)

_CONTEXT_METRIC_KEYS = ("rules_bytes", "spine_bytes", "documents_count")
_LIST_FILES_METRIC_KEYS = ("count", "scanned_count", "has_more")
_READ_FILE_METRIC_KEYS = ("representation", "context_support")


def _get_structured_content(result: CallToolResult) -> dict[str, object] | None:
    sc = result.structuredContent
    if isinstance(sc, dict):
        return sc
    return None


def _result_bytes(result: CallToolResult) -> int:
    """Return the serialized MCP result size, including structured content."""
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))


def _envelope_data(structured: dict[str, object] | None) -> dict[object, object] | None:
    if structured is None:
        return None
    data = structured.get("data")
    if not isinstance(data, dict):
        return None
    return cast("dict[object, object]", data)


def _pick(source: dict[object, object], keys: tuple[str, ...]) -> JsonObject:
    metrics: JsonObject = {}
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool | int | float | str):
            metrics[key] = value
    return metrics


def _context_metrics(structured: dict[str, object] | None) -> JsonObject | None:
    """Pull get_context payload telemetry out of a result envelope."""
    data = _envelope_data(structured)
    if data is None:
        return None
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return None
    return _pick(cast("dict[object, object]", payload), _CONTEXT_METRIC_KEYS) or None


def _list_files_metrics(structured: dict[str, object] | None) -> JsonObject | None:
    """Result count and walk cost for a discovery call."""
    data = _envelope_data(structured)
    if data is None:
        return None
    return _pick(data, _LIST_FILES_METRIC_KEYS) or None


def _read_file_metrics(structured: dict[str, object] | None) -> JsonObject | None:
    """Original vs rendition sizes for a file read — never file content."""
    data = _envelope_data(structured)
    if data is None:
        return None
    metrics = _pick(data, _READ_FILE_METRIC_KEYS)
    original = data.get("original")
    if isinstance(original, dict):
        for key, value in _pick(
            cast("dict[object, object]", original), ("mime_type", "size_bytes")
        ).items():
            metrics[f"original_{key}"] = value
    rendition = data.get("rendition")
    if isinstance(rendition, dict):
        for key, value in _pick(
            cast("dict[object, object]", rendition), ("mime_type", "size_bytes", "width", "height")
        ).items():
            metrics[f"rendition_{key}"] = value
    return metrics or None


#: Per-tool telemetry extractors. Everything here is a count, a size, or a
#: classification — never argument values, file content, or blobs.
_METRIC_EXTRACTORS: dict[str, Callable[[dict[str, object] | None], JsonObject | None]] = {
    "get_context": _context_metrics,
    "list_files": _list_files_metrics,
    "read_file": _read_file_metrics,
}


def _record(
    *,
    tool_name: str,
    correlation_id: str,
    project: str | None,
    ok: bool | None,
    error_code: str | None,
    argument_keys: list[str],
    duration_ms: float | None,
    result_bytes: int | None,
    context_metrics: JsonObject | None,
) -> None:
    try:
        db = require_database()
        conn = db.get_connection()
        try:
            record_mcp_call_observation(
                conn,
                tool_name=tool_name,
                correlation_id=correlation_id,
                project_key=project,
                ok=ok,
                error_code=error_code,
                transport=current_transport(),
                argument_keys=argument_keys,
                context_metrics=context_metrics,
                duration_ms=duration_ms,
                result_bytes=result_bytes,
            )
        finally:
            conn.close()
    except Exception as exc:  # observation must never break the tool call
        logger.error(
            "Failed to record MCP call observation for %r (type=%s)",
            tool_name,
            type(exc).__name__,
        )


def _record_unhandled_error(
    *,
    tool_name: str,
    correlation_id: str,
    project: str | None,
    argument_keys: list[str],
    started: float,
    exc: Exception,
) -> CallToolResult:
    _record(
        tool_name=tool_name,
        correlation_id=correlation_id,
        project=project,
        ok=False,
        error_code="INTERNAL_ERROR",
        argument_keys=argument_keys,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        result_bytes=None,
        context_metrics=None,
    )
    logger.error(
        "Unhandled exception in MCP tool %r (correlation_id=%s, type=%s)",
        tool_name,
        correlation_id,
        type(exc).__name__,
    )
    return make_error(
        "INTERNAL_ERROR",
        "Lattice encountered an unexpected internal error",
        {"correlation_id": correlation_id},
        project=project,
    )


def _record_completed_call(
    *,
    tool_name: str,
    correlation_id: str,
    project: str | None,
    argument_keys: list[str],
    started: float,
    result: object,
) -> None:
    ok_val: bool | None = None
    error_code: str | None = None
    result_bytes: int | None = None
    context_metrics: JsonObject | None = None
    if isinstance(result, CallToolResult):
        structured = _get_structured_content(result)
        if structured is not None:
            raw_ok = structured.get("ok")
            if isinstance(raw_ok, bool):
                ok_val = raw_ok
            raw_error = structured.get("error_code")
            if isinstance(raw_error, str) and raw_error:
                error_code = raw_error
        if ok_val is None:
            ok_val = not result.isError
        result_bytes = _result_bytes(result)
        extractor = _METRIC_EXTRACTORS.get(tool_name)
        if extractor is not None:
            context_metrics = extractor(structured)

    _record(
        tool_name=tool_name,
        correlation_id=correlation_id,
        project=project,
        ok=ok_val,
        error_code=error_code,
        argument_keys=argument_keys,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        result_bytes=result_bytes,
        context_metrics=context_metrics,
    )


def observe_tool(tool_name: str, func: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap an MCP tool function to record call observations."""

    if inspect.iscoroutinefunction(func):

        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            correlation_id = new_correlation_id()
            project = kwargs.get("project")
            if not isinstance(project, str):
                project = None
            argument_keys = sorted(kwargs.keys())
            started = time.perf_counter()
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                return _record_unhandled_error(
                    tool_name=tool_name,
                    correlation_id=correlation_id,
                    project=project,
                    argument_keys=argument_keys,
                    started=started,
                    exc=exc,
                )
            _record_completed_call(
                tool_name=tool_name,
                correlation_id=correlation_id,
                project=project,
                argument_keys=argument_keys,
                started=started,
                result=result,
            )
            return result

        async_wrapper.__dict__["__lattice_observed__"] = True
        return async_wrapper

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        correlation_id = new_correlation_id()
        project = kwargs.get("project")
        if not isinstance(project, str):
            project = None
        argument_keys = sorted(kwargs.keys())
        started = time.perf_counter()
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            return _record_unhandled_error(
                tool_name=tool_name,
                correlation_id=correlation_id,
                project=project,
                argument_keys=argument_keys,
                started=started,
                exc=exc,
            )
        _record_completed_call(
            tool_name=tool_name,
            correlation_id=correlation_id,
            project=project,
            argument_keys=argument_keys,
            started=started,
            result=result,
        )
        return result

    # FastMCP exposes its registered callables dynamically, so Any is required
    # at this private framework boundary. Mark wrappers to keep repeated
    # ``serve()`` calls from nesting observation layers.
    wrapper.__dict__["__lattice_observed__"] = True
    return wrapper


def apply_observation_to_all_tools(mcp: Any) -> None:
    """Wrap every registered MCP tool with call observation.

    Must be called **after** all ``register_*_tools()`` functions.
    """
    tool_mgr = getattr(mcp, "_tool_manager", None)
    if tool_mgr is None:
        logger.warning("MCP tool manager not found; observation not applied")
        return
    tools: dict[str, Any] | None = getattr(tool_mgr, "_tools", None)
    if tools is None:
        logger.warning("MCP tools dict not found; observation not applied")
        return
    for tool_name, tool in tools.items():
        original_fn = getattr(tool, "fn", None)
        if original_fn is None:
            continue
        if getattr(original_fn, "__lattice_observed__", False):
            continue
        tool.fn = observe_tool(tool_name, original_fn)
    logger.info("Applied MCP call observation to %d tool(s)", len(tools))
