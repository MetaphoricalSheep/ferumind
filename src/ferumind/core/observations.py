"""MCP call observation recording and retrieval (spec-mcp §8).

Records every MCP tool call at a metadata level — separate from the
operation log, which records workspace mutations. Observation rows capture
the tool name, a server-generated correlation id, success/error, timing and
payload sizes, argument keys (not values), and any metadata the MCP client
exposed. Never stores document content, patch bodies, full request JSON, or
full tool results. Secrets are replaced with ``[redacted]``.

Identifier prefixes are cosmetic. Nothing parses, validates, or dispatches on
them: rows are looked up by whole-string equality, and the column name already
says what kind of id it holds. New ids therefore carry ``ID_PREFIX``, while
rows written under the project's former name keep their ``lat_`` prefix
unchanged and stay readable — there is no migration and no dual-prefix branch
anywhere. Do not introduce a reader that assumes either form.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from ferumind.core.types import DbConnection, DbRow, JsonObject, StrictModel

ID_PREFIX = "fm"

SERVER_BOOT_ID = f"{ID_PREFIX}_boot_{secrets.token_urlsafe(16)}"
PROCESS_ID = os.getpid()

_CAP_CONTEXT_METRICS = 4 * 1024
_CAP_ARGUMENT_KEYS = 4 * 1024
_CAP_REDACTION_NOTES = 8 * 1024

_REDACT_KEYS = re.compile(
    r"^(context_token|authorization|api_key|token|secret|password|cookie|set-cookie)$",
    re.IGNORECASE,
)


class McpCallObservationRecord(StrictModel):
    id: str
    correlation_id: str
    tool_name: str
    project_key: str | None = None
    created_at: str
    ok: bool | None = None
    error_code: str | None = None
    transport: str | None = None
    server_boot_id: str
    process_id: int
    client_name: str | None = None
    client_version: str | None = None
    protocol_version: str | None = None
    duration_ms: float | None = None
    result_bytes: int | None = None
    context_metrics_json: str = "{}"
    argument_keys_json: str = "[]"
    redaction_notes_json: str = "[]"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_correlation_id() -> str:
    return f"{ID_PREFIX}_corr_{uuid.uuid4().hex}"


def _observation_id() -> str:
    return f"{ID_PREFIX}_obs_{uuid.uuid4().hex}"


def _redact_metadata(raw: JsonObject | None) -> tuple[JsonObject, list[str]]:
    if not raw:
        return {}, []
    redacted: JsonObject = {}
    notes: list[str] = []
    for key, value in raw.items():
        if _REDACT_KEYS.match(key):
            redacted[key] = "[redacted]"
            notes.append(f"redacted key={key!r}")
        else:
            redacted[key] = value
    return redacted, notes


def _cap_json(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    marker = json.dumps(
        {
            "__truncated__": True,
            "original_size_bytes": len(encoded),
        },
        separators=(",", ":"),
    )
    if len(marker.encode("utf-8")) <= max_bytes:
        return marker
    return "{}"


def _safe_serialize(obj: object) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps({"__serialization_error__": True})


def record_mcp_call_observation(
    conn: DbConnection,
    *,
    tool_name: str,
    correlation_id: str | None = None,
    project_key: str | None = None,
    ok: bool | None = None,
    error_code: str | None = None,
    transport: str | None = None,
    argument_keys: list[str] | None = None,
    context_metrics: JsonObject | None = None,
    client_name: str | None = None,
    client_version: str | None = None,
    protocol_version: str | None = None,
    duration_ms: float | None = None,
    result_bytes: int | None = None,
) -> str:
    """Record one MCP call observation; returns the observation id."""
    obs_id = _observation_id()
    redacted_metrics, notes = _redact_metadata(context_metrics)
    context_metrics_raw = _cap_json(_safe_serialize(redacted_metrics), _CAP_CONTEXT_METRICS)
    argument_keys_raw = _cap_json(_safe_serialize(argument_keys or []), _CAP_ARGUMENT_KEYS)
    redaction_notes_raw = _cap_json(_safe_serialize(notes), _CAP_REDACTION_NOTES)

    conn.execute(
        """INSERT INTO mcp_call_observations
           (id, correlation_id, tool_name, project_key, created_at,
            ok, error_code, transport, server_boot_id, process_id,
            client_name, client_version, protocol_version,
            duration_ms, result_bytes,
            context_metrics_json, argument_keys_json, redaction_notes_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            obs_id,
            correlation_id or new_correlation_id(),
            tool_name,
            project_key,
            _now_iso(),
            1 if ok else (0 if ok is False else None),
            error_code,
            transport,
            SERVER_BOOT_ID,
            PROCESS_ID,
            client_name,
            client_version,
            protocol_version,
            duration_ms,
            result_bytes,
            context_metrics_raw,
            argument_keys_raw,
            redaction_notes_raw,
        ),
    )
    conn.commit()
    return obs_id


def list_observations(
    conn: DbConnection,
    *,
    limit: int = 50,
    tool_name: str | None = None,
    project_key: str | None = None,
) -> Sequence[McpCallObservationRecord]:
    if tool_name is not None and project_key is not None:
        rows = conn.execute(
            """SELECT * FROM mcp_call_observations
               WHERE tool_name = ? AND project_key = ?
               ORDER BY created_at DESC LIMIT ?""",
            (tool_name, project_key, limit),
        ).fetchall()
    elif tool_name is not None:
        rows = conn.execute(
            """SELECT * FROM mcp_call_observations
               WHERE tool_name = ?
               ORDER BY created_at DESC LIMIT ?""",
            (tool_name, limit),
        ).fetchall()
    elif project_key is not None:
        rows = conn.execute(
            """SELECT * FROM mcp_call_observations
               WHERE project_key = ?
               ORDER BY created_at DESC LIMIT ?""",
            (project_key, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM mcp_call_observations ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_observation(row) for row in rows]


def get_observation(conn: DbConnection, call_id: str) -> McpCallObservationRecord | None:
    row = conn.execute("SELECT * FROM mcp_call_observations WHERE id = ?", (call_id,)).fetchone()
    if row is None:
        return None
    return _row_to_observation(row)


def get_observation_by_correlation_id(
    conn: DbConnection, correlation_id: str
) -> McpCallObservationRecord | None:
    """Return the newest row for an opaque correlation id, if one exists.

    Correlation ids are intentionally indexed but not constrained unique.
    Existing databases may contain duplicate historical values, so the
    deterministic newest-row rule keeps incident lookup useful without
    silently turning a diagnostic migration into a data-cleanup operation.
    Prefixes such as ``fm_corr_`` and ``lat_corr_`` have no semantics here.
    """

    row = conn.execute(
        """SELECT * FROM mcp_call_observations
           WHERE correlation_id = ?
           ORDER BY created_at DESC, id DESC
           LIMIT 1""",
        (correlation_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_observation(row)


def _row_to_observation(row: DbRow) -> McpCallObservationRecord:
    ok_val = row["ok"]
    return McpCallObservationRecord(
        id=row["id"],
        correlation_id=row["correlation_id"],
        tool_name=row["tool_name"],
        project_key=row["project_key"],
        created_at=row["created_at"],
        ok=None if ok_val is None else bool(ok_val),
        error_code=row["error_code"],
        transport=row["transport"],
        server_boot_id=row["server_boot_id"],
        process_id=row["process_id"],
        client_name=row["client_name"],
        client_version=row["client_version"],
        protocol_version=row["protocol_version"],
        duration_ms=row["duration_ms"],
        result_bytes=row["result_bytes"],
        context_metrics_json=row["context_metrics_json"],
        argument_keys_json=row["argument_keys_json"],
        redaction_notes_json=row["redaction_notes_json"],
    )
