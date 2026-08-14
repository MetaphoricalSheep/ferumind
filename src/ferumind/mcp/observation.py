"""MCP call observation, as server middleware (spec-mcp §8).

Records metadata for every inbound ``tools/call`` and ``resources/read``:
method, correlation id, project, ok/error code, transport, client identity,
duration, result size, and argument *keys* — never argument values, document
content, or blobs. ``get_context``, ``list_files``, and ``read_file`` also
record their payload telemetry. Observation failures are swallowed and logged
so telemetry can never break a user call.

Why middleware
--------------
This used to monkey-patch ``fn`` on every registered tool, reached through the
SDK's private tool registry. mcp 2.x makes ``middleware=`` a public
``MCPServer`` constructor parameter, so observation is now declared with the
server itself. Three consequences, all improvements:

* **A server cannot start unobserved.** The old wrapper had to fail closed at
  startup precisely because it might not attach; a constructor argument either
  applies or the process does not exist.
* **``resources/read`` is covered by the same code.** It previously carried a
  hand-written copy of this bookkeeping.
* **Client identity is recorded.** ``client_name``, ``client_version``, and
  ``protocol_version`` are columns that were always null on stdio; the request
  context exposes them.

Sanitising a crashing tool is *not* here — the SDK converts an exception to
client-visible text one frame below this middleware, so it has to be caught
inside the invocation seam. See :mod:`ferumind.mcp.tool_boundary`.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from ferumind.core.errors import FerumindError
from ferumind.core.file_uri import FILE_URI_PREFIX, parse_file_uri
from ferumind.core.observations import new_correlation_id, record_mcp_call_observation
from ferumind.core.runtime_events import (
    ClientInitializedEvent,
    TelemetryFailureStage,
    observation_write_failed_event,
    try_append_runtime_event,
)
from ferumind.core.types import JsonObject
from ferumind.mcp.tool_context import current_transport, require_database, require_workspace

logger = logging.getLogger(__name__)

_CONTEXT_METRIC_KEYS = (
    "rules_bytes",
    "spine_bytes",
    "documents_count",
    "skills_bytes",
    "descriptions_bytes",
)
_LIST_FILES_METRIC_KEYS = ("count", "scanned_count", "has_more")
_READ_FILE_METRIC_KEYS = ("representation", "context_support")

#: Methods this middleware records. Everything else — ``initialize``,
#: ``tools/list``, notifications — passes through untouched.
_TOOL_CALL = "tools/call"
_RESOURCE_READ = "resources/read"
_OBSERVED_METHODS = frozenset({_TOOL_CALL, _RESOURCE_READ})

#: Argument keys arrive before validation, so their names are caller-supplied.
#: The observation writer caps the serialized column, but bounding here keeps a
#: junk-key flood out of the log entirely rather than truncating a real call's
#: keys away.
MAX_ARGUMENT_KEYS = 64
MAX_ARGUMENT_KEY_CHARS = 64

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("ferumind_correlation_id")


def _safe_error_log(message: str, *args: object) -> None:
    """Keep even a broken logging handler outside the MCP result path."""

    try:
        logger.error(message, *args)
    except Exception:  # Logging is telemetry and must never alter an MCP call.
        return


@dataclass(frozen=True)
class _ObservedCall:
    """Bounded request metadata shared by every telemetry stage."""

    tool_name: str
    correlation_id: str
    project: str | None
    argument_keys: tuple[str, ...]


@dataclass(frozen=True)
class _ResultInterpretation:
    """Safe outcome fields extracted from a serialized MCP result."""

    ok: bool | None
    error_code: str | None
    structured: JsonObject | None


@dataclass(frozen=True)
class _ObservationWrite:
    """One complete observation row ready for persistence."""

    call: _ObservedCall
    ok: bool | None
    error_code: str | None
    duration_ms: float | None
    result_bytes: int | None
    context_metrics: JsonObject | None
    client_name: str | None
    client_version: str | None
    protocol_version: str | None


@dataclass(frozen=True)
class _CallInFlight:
    """Metadata and monotonic start time for one observed request."""

    method: str
    call: _ObservedCall
    started: float | None


def current_correlation_id() -> str:
    """The correlation id for the call in flight.

    Set by the middleware so a sanitised ``INTERNAL_ERROR`` envelope quotes the
    same id as its observation row. Falls back to a fresh id when a tool is
    invoked outside the protocol path (unit tests calling ``tool.fn`` directly),
    which keeps the envelope well-formed rather than raising inside an error
    handler.
    """
    return _correlation_id.get(None) or new_correlation_id()


def _envelope_data(structured: JsonObject | None) -> dict[str, object] | None:
    if structured is None:
        return None
    data = structured.get("data")
    if not isinstance(data, dict):
        return None
    return cast("dict[str, object]", data)


def _pick(source: dict[str, object], keys: tuple[str, ...]) -> JsonObject:
    metrics: JsonObject = {}
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool | int | float | str):
            metrics[key] = value
    return metrics


def _context_metrics(structured: JsonObject | None) -> JsonObject | None:
    """Pull get_context payload telemetry out of a result envelope."""
    data = _envelope_data(structured)
    if data is None:
        return None
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return None
    return _pick(cast("dict[str, object]", payload), _CONTEXT_METRIC_KEYS) or None


def _list_files_metrics(structured: JsonObject | None) -> JsonObject | None:
    """Result count and walk cost for a discovery call."""
    data = _envelope_data(structured)
    if data is None:
        return None
    return _pick(data, _LIST_FILES_METRIC_KEYS) or None


def _read_file_metrics(structured: JsonObject | None) -> JsonObject | None:
    """Original vs rendition sizes for a file read — never file content."""
    data = _envelope_data(structured)
    if data is None:
        return None
    metrics = _pick(data, _READ_FILE_METRIC_KEYS)
    original = data.get("original")
    if isinstance(original, dict):
        for key, value in _pick(
            cast("dict[str, object]", original), ("mime_type", "size_bytes")
        ).items():
            metrics[f"original_{key}"] = value
    rendition = data.get("rendition")
    if isinstance(rendition, dict):
        for key, value in _pick(
            cast("dict[str, object]", rendition), ("mime_type", "size_bytes", "width", "height")
        ).items():
            metrics[f"rendition_{key}"] = value
    return metrics or None


#: Per-tool telemetry extractors. Everything here is a count, a size, or a
#: classification — never argument values, file content, or blobs.
_METRIC_EXTRACTORS: dict[str, Callable[[JsonObject | None], JsonObject | None]] = {
    "get_context": _context_metrics,
    "list_files": _list_files_metrics,
    "read_file": _read_file_metrics,
}


def _record(write: _ObservationWrite) -> None:
    """Persist one row, degrading to the private runtime stream on failure."""

    try:
        db = require_database()
        conn = db.get_connection()
        try:
            record_mcp_call_observation(
                conn,
                tool_name=write.call.tool_name,
                correlation_id=write.call.correlation_id,
                project_key=write.call.project,
                ok=write.ok,
                error_code=write.error_code,
                transport=current_transport(),
                argument_keys=list(write.call.argument_keys),
                context_metrics=write.context_metrics,
                client_name=write.client_name,
                client_version=write.client_version,
                protocol_version=write.protocol_version,
                duration_ms=write.duration_ms,
                result_bytes=write.result_bytes,
            )
        finally:
            conn.close()
    except Exception as exc:  # observation must never break the call
        _report_telemetry_failure(exc, write.call, "observation_persistence")


def _report_telemetry_failure(
    exc: BaseException,
    call: _ObservedCall,
    stage: TelemetryFailureStage,
) -> None:
    """Best-effort typed fallback that cannot propagate into an MCP call."""

    try:
        event = observation_write_failed_event(
            exc,
            correlation_id=call.correlation_id,
            tool_name=call.tool_name,
            stage=stage,
        )
        _safe_error_log(
            "MCP telemetry failed (correlation_id=%s, tool=%s, stage=%s, type=%s)",
            event.correlation_id,
            event.tool_name,
            event.stage,
            event.exception_type,
        )
        try_append_runtime_event(require_workspace(), event)
    except Exception as fallback_exc:  # The fallback is also telemetry.
        _safe_error_log(
            "Failed to persist the MCP telemetry fallback (type=%s)",
            type(fallback_exc).__name__,
        )


def _bounded_argument_keys(arguments: object) -> list[str]:
    """Sorted argument key names, bounded — these are pre-validation and hostile."""
    if not isinstance(arguments, dict):
        return []
    keys = sorted(str(key)[:MAX_ARGUMENT_KEY_CHARS] for key in cast("dict[str, object]", arguments))
    return keys[:MAX_ARGUMENT_KEYS]


def _project_from_arguments(arguments: object) -> str | None:
    if not isinstance(arguments, dict):
        return None
    project = cast("dict[str, object]", arguments).get("project")
    return project if isinstance(project, str) else None


def _project_from_uri(uri: object) -> str | None:
    """Best-effort project key for a resource read; never raises."""
    if not isinstance(uri, str) or not uri.startswith(FILE_URI_PREFIX):
        return None
    try:
        return parse_file_uri(uri).project_key
    except FerumindError:
        return None


def _resource_metrics(result: dict[str, object]) -> JsonObject | None:
    """MIME type and byte count for a resource read — never the bytes."""
    contents = result.get("contents")
    if not isinstance(contents, list) or not contents:
        return None
    first = cast("list[object]", contents)[0]
    if not isinstance(first, dict):
        return None
    entry = cast("dict[str, object]", first)
    metrics: JsonObject = {}
    mime = entry.get("mimeType")
    if isinstance(mime, str):
        metrics["mime_type"] = mime
    text, blob = entry.get("text"), entry.get("blob")
    if isinstance(text, str):
        metrics["kind"] = "text"
        metrics["size_bytes"] = len(text.encode("utf-8"))
    elif isinstance(blob, str):
        metrics["kind"] = "blob"
        # base64 expands 3 bytes to 4; report the decoded size.
        metrics["size_bytes"] = len(blob) // 4 * 3
    return metrics or None


def _serialized_bytes(result: object) -> int | None:
    """Exact size of the result as it goes on the wire.

    No ``default=`` coercion: ``call_next`` hands back a result already dumped
    in JSON mode, so anything unserializable means the shape is not what this
    measured. Recording ``NULL`` is honest; stringifying it would report a
    confident wrong number.
    """
    try:
        return _measure_serialized_bytes(result)
    except (TypeError, ValueError):
        return None


def _measure_serialized_bytes(result: object) -> int:
    """Measure the existing wire form, allowing containment at the caller."""

    return len(json.dumps(result, separators=(",", ":")).encode("utf-8"))


class CallObservationMiddleware:
    """Record one observation row per observed inbound request.

    Installed through ``MCPServer(middleware=[...])``. Runs inside the SDK's own
    OpenTelemetry and request-state middleware, so it sees plaintext params and
    the fully-formed result.
    """

    async def __call__(
        self,
        ctx: Any,
        call_next: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """``ctx`` and ``call_next`` are ``Any``: the SDK's ``ServerMiddleware``
        protocol types are generic in a lifespan parameter Ferumind never uses,
        and binding them here would leak that generic through the whole module
        for no checking benefit. Every field read below is guarded by isinstance.
        """
        method = getattr(ctx, "method", None)
        if method not in _OBSERVED_METHODS:
            return await call_next(ctx)

        raw_params = getattr(ctx, "params", None)
        params: dict[str, object] = (
            cast("dict[str, object]", raw_params) if isinstance(raw_params, dict) else {}
        )

        correlation_id = new_correlation_id()
        call = _safe_observed_call(method, params, correlation_id)
        in_flight = _CallInFlight(method=method, call=call, started=_safe_started(call))
        token = _correlation_id.set(correlation_id)
        try:
            try:
                result = await call_next(ctx)
            except Exception as exc:
                # Protocol-level failures keep their original object and wire
                # contract even if every telemetry stage also fails.
                try:
                    self._observe_exception(
                        ctx,
                        in_flight,
                        exc,
                        _safe_duration(in_flight),
                    )
                except Exception as telemetry_exc:
                    _report_telemetry_failure(
                        telemetry_exc,
                        call,
                        "observation_persistence",
                    )
                raise
        finally:
            _correlation_id.reset(token)

        duration_ms = _safe_duration(in_flight)
        try:
            self._observe_result(ctx, in_flight, result, duration_ms)
        except Exception as exc:
            # Last-resort structural guard. Individual stages below already
            # degrade independently; this protects against future telemetry
            # code being added without its own containment.
            _report_telemetry_failure(exc, call, "observation_persistence")
        return result

    @staticmethod
    def _observe_result(
        ctx: object,
        in_flight: _CallInFlight,
        result: object,
        duration_ms: float | None,
    ) -> None:
        interpretation = _safe_interpret(in_flight, result)
        metrics = _safe_metrics(in_flight, result, interpretation)
        identity = _safe_client_identity(ctx, in_flight.call)
        _record(
            _ObservationWrite(
                call=in_flight.call,
                ok=interpretation.ok,
                error_code=interpretation.error_code,
                duration_ms=duration_ms,
                result_bytes=_safe_result_bytes(in_flight.call, result),
                context_metrics=metrics,
                client_name=identity[0],
                client_version=identity[1],
                protocol_version=identity[2],
            )
        )

    @staticmethod
    def _observe_exception(
        ctx: object,
        in_flight: _CallInFlight,
        exc: BaseException,
        duration_ms: float | None,
    ) -> None:
        identity = _safe_client_identity(ctx, in_flight.call)
        _record(
            _ObservationWrite(
                call=in_flight.call,
                ok=False,
                error_code=type(exc).__name__,
                duration_ms=duration_ms,
                result_bytes=None,
                context_metrics=None,
                client_name=identity[0],
                client_version=identity[1],
                protocol_version=identity[2],
            )
        )


def _observed_call(
    method: str,
    params: dict[str, object],
    correlation_id: str,
) -> _ObservedCall:
    if method == _TOOL_CALL:
        raw_name = params.get("name")
        tool_name = raw_name if isinstance(raw_name, str) else "<unknown>"
        arguments = params.get("arguments")
        keys = tuple(_bounded_argument_keys(arguments))
        project = _project_from_arguments(arguments)
    else:
        tool_name = _RESOURCE_READ
        keys = ("uri",)
        project = _project_from_uri(params.get("uri"))
    return _ObservedCall(
        tool_name=tool_name,
        correlation_id=correlation_id,
        project=project,
        argument_keys=keys,
    )


def _safe_observed_call(
    method: str,
    params: dict[str, object],
    correlation_id: str,
) -> _ObservedCall:
    fallback = _ObservedCall(
        tool_name=_RESOURCE_READ if method == _RESOURCE_READ else "<unknown>",
        correlation_id=correlation_id,
        project=None,
        argument_keys=(),
    )
    try:
        return _observed_call(method, params, correlation_id)
    except Exception as exc:
        _report_telemetry_failure(exc, fallback, "result_interpretation")
        return fallback


def _interpret_result(method: str, result: object) -> _ResultInterpretation:
    """Read outcome fields from the serialized result without metrics.

    ``call_next`` returns the camelCase wire dict, not ``CallToolResult``.
    Resources/read classification remains exactly as it was before telemetry
    failure containment was split into stages.
    """

    if not isinstance(result, dict):
        return _ResultInterpretation(ok=None, error_code=None, structured=None)
    wire = cast("dict[str, object]", result)
    if method == _RESOURCE_READ:
        return _ResultInterpretation(ok=True, error_code=None, structured=None)

    structured_raw = wire.get("structuredContent")
    structured = cast("JsonObject", structured_raw) if isinstance(structured_raw, dict) else None
    ok, error_code = _envelope_outcome(structured)
    if ok is None:
        ok = not bool(wire.get("isError"))
    return _ResultInterpretation(ok=ok, error_code=error_code, structured=structured)


def _envelope_outcome(structured: JsonObject | None) -> tuple[bool | None, str | None]:
    if structured is None:
        return None, None
    raw_ok = structured.get("ok")
    ok = raw_ok if isinstance(raw_ok, bool) else None
    raw_error = structured.get("error_code")
    error_code = raw_error if isinstance(raw_error, str) and raw_error else None
    return ok, error_code


def _extract_metrics(
    in_flight: _CallInFlight,
    result: object,
    interpretation: _ResultInterpretation,
) -> JsonObject | None:
    if in_flight.method == _RESOURCE_READ:
        wire = cast("dict[str, object]", result) if isinstance(result, dict) else {}
        return _resource_metrics(wire)
    extractor = _METRIC_EXTRACTORS.get(in_flight.call.tool_name)
    if extractor is None:
        return None
    return extractor(interpretation.structured)


def _safe_interpret(
    in_flight: _CallInFlight,
    result: object,
) -> _ResultInterpretation:
    try:
        return _interpret_result(in_flight.method, result)
    except Exception as exc:
        _report_telemetry_failure(exc, in_flight.call, "result_interpretation")
        return _ResultInterpretation(ok=None, error_code=None, structured=None)


def _safe_metrics(
    in_flight: _CallInFlight,
    result: object,
    interpretation: _ResultInterpretation,
) -> JsonObject | None:
    try:
        return _extract_metrics(in_flight, result, interpretation)
    except Exception as exc:
        _report_telemetry_failure(exc, in_flight.call, "metric_extraction")
        return None


def _safe_duration(in_flight: _CallInFlight) -> float | None:
    if in_flight.started is None:
        return None
    try:
        return (time.perf_counter() - in_flight.started) * 1000.0
    except Exception as exc:
        _report_telemetry_failure(exc, in_flight.call, "result_interpretation")
        return None


def _safe_started(call: _ObservedCall) -> float | None:
    try:
        return time.perf_counter()
    except Exception as exc:
        _report_telemetry_failure(exc, call, "result_interpretation")
        return None


def _safe_result_bytes(call: _ObservedCall, result: object) -> int | None:
    try:
        measured = _serialized_bytes(result)
        if measured is None:
            # Re-run only the failure case so the fallback can retain the safe
            # exception type while preserving the long-standing helper API.
            _measure_serialized_bytes(result)
        return measured
    except Exception as exc:
        _report_telemetry_failure(exc, call, "result_size")
        return None


def _safe_client_identity(
    ctx: object,
    call: _ObservedCall,
) -> tuple[str | None, str | None, str | None]:
    try:
        client_name, client_version = _client_identity(ctx)
        protocol_version = getattr(ctx, "protocol_version", None)
        return (
            client_name,
            client_version,
            protocol_version if isinstance(protocol_version, str) else None,
        )
    except Exception as exc:
        _report_telemetry_failure(exc, call, "client_identity")
        return None, None, None


class LifecycleEventMiddleware:
    """Persist only a successful initialize's safe client metadata."""

    async def __call__(
        self,
        ctx: Any,
        call_next: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        """The SDK context is generic; all values read here are type-guarded."""

        result = await call_next(ctx)
        if getattr(ctx, "method", None) == "initialize":
            _record_client_initialized(ctx, result)
        return result


def _record_client_initialized(ctx: object, result: object) -> None:
    """Best-effort lifecycle write; never retain the initialize payload."""

    try:
        client_name, client_version, protocol_version = _initialize_identity(ctx, result)
        event = ClientInitializedEvent(
            client_name=client_name,
            client_version=client_version,
            protocol_version=protocol_version,
        )
        try_append_runtime_event(require_workspace(), event)
    except Exception as exc:
        _safe_error_log(
            "Failed to persist client initialization metadata (type=%s)",
            type(exc).__name__,
        )


def _initialize_identity(
    ctx: object,
    result: object,
) -> tuple[str | None, str | None, str | None]:
    raw_params = getattr(ctx, "params", None)
    if not isinstance(raw_params, Mapping):
        return None, None, None
    params = cast("Mapping[object, object]", raw_params)
    protocol = _result_protocol_version(result)
    if protocol is None:
        protocol = _bounded_optional_text(params.get("protocolVersion"), 128)
    raw_info = params.get("clientInfo")
    if not isinstance(raw_info, Mapping):
        return None, None, protocol
    info = cast("Mapping[object, object]", raw_info)
    return (
        _bounded_optional_text(info.get("name"), 256),
        _bounded_optional_text(info.get("version"), 128),
        protocol,
    )


def _result_protocol_version(result: object) -> str | None:
    if not isinstance(result, Mapping):
        return None
    result_mapping = cast("Mapping[object, object]", result)
    return _bounded_optional_text(result_mapping.get("protocolVersion"), 128)


def _bounded_optional_text(value: object, max_length: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned = "".join(character for character in value if character.isprintable())
    return cleaned[:max_length] or None


def _client_identity(ctx: object) -> tuple[str | None, str | None]:
    """Client name and version when the transport exposes them, else nulls.

    Never guessed: an absent value stays null rather than becoming a default.
    """
    session = getattr(ctx, "session", None)
    client_params = getattr(session, "client_params", None)
    client_info = getattr(client_params, "client_info", None)
    name = getattr(client_info, "name", None)
    version = getattr(client_info, "version", None)
    return (
        name if isinstance(name, str) else None,
        version if isinstance(version, str) else None,
    )
