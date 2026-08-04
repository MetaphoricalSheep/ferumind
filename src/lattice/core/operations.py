"""Operation log: audit trail for every mutation plus pending patch proposals.

Proposal rows (``operation_type`` in :data:`PROPOSAL_OP_TYPES`) are keyed by
an unguessable operation id and bound to ``project`` + ``path`` +
``base_sha256`` with a 24 h TTL (spec-mcp §5.2). States:

- ``pending`` — proposed, not yet applied
- ``applied`` — consumed by ``apply_patch`` (also the terminal state of
  direct writes and audit entries that succeeded)
- ``discarded`` — withdrawn via ``discard_patch``
- ``stale`` — invalidated by an out-of-band edit of the target
- ``expired`` — past the 24 h TTL
- ``failed`` — an attempted mutation that did not complete

There is no session binding anywhere: the operation id plus content hashes
are the whole continuity (00 D8).
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Final, cast

from pydantic import BaseModel, ConfigDict, Field

from lattice.core.errors import InvalidOperationError, ValidationError
from lattice.core.types import DbConnection, DbRow, JsonMapping, JsonObject

OP_PENDING: Final = "pending"
OP_APPLIED: Final = "applied"
OP_DISCARDED: Final = "discarded"
OP_STALE: Final = "stale"
OP_EXPIRED: Final = "expired"
OP_FAILED: Final = "failed"

OP_STATES: Final[frozenset[str]] = frozenset(
    {OP_PENDING, OP_APPLIED, OP_DISCARDED, OP_STALE, OP_EXPIRED, OP_FAILED}
)

#: Operation sources: agent-driven MCP calls, out-of-band disk edits noticed
#: by reconcile-on-read, watcher detections, and CLI actions.
SOURCE_AGENT: Final = "agent"
SOURCE_OUT_OF_BAND: Final = "out-of-band"
SOURCE_WATCHER: Final = "watcher"
SOURCE_CLI: Final = "cli"

#: Reserved operation-log scope for workspace-level mutations such as compacts.
#: It is not a user project and must never appear in project administration.
WORKSPACE_OPERATION_PROJECT: Final = "__workspace__"

PROPOSAL_TTL: Final = timedelta(hours=24)
MAX_PENDING_OPERATIONS_PER_PROJECT: Final = 1000
MAX_PENDING_OPERATION_BYTES_PER_PROJECT: Final = 64 * 1024 * 1024

PROPOSAL_OP_TYPES: Final[frozenset[str]] = frozenset(
    {
        "propose_patch",
        "propose_section_patch",
        "propose_range_patch",
        "propose_search_replace_patch",
        "propose_exact_replace_patch",
        "propose_multi_edit_patch",
        "propose_frontmatter_patch",
        "propose_insert_patch",
    }
)


class OperationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_key: str
    operation_type: str
    tool_name: str | None = None
    target_path: str | None = None
    source: str = SOURCE_AGENT
    request_json: JsonObject = Field(default_factory=dict)
    base_sha256: str | None = None
    after_sha256: str | None = None
    diff_text: str | None = None
    snapshot_id: str | None = None
    state: str
    created_at: str
    expires_at: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def new_proposal_id() -> str:
    """Return an unguessable (≥128-bit random) proposal operation id."""
    return f"op_{secrets.token_urlsafe(24)}"


def new_audit_id() -> str:
    return str(uuid.uuid4())


def record_operation(
    conn: DbConnection,
    *,
    project_key: str,
    operation_type: str,
    tool_name: str | None = None,
    target_path: str | None = None,
    source: str = SOURCE_AGENT,
    request_json: JsonMapping | None = None,
    base_sha256: str | None = None,
    after_sha256: str | None = None,
    diff_text: str | None = None,
    snapshot_id: str | None = None,
    state: str = OP_APPLIED,
    expires_at: str | None = None,
    operation_id: str | None = None,
    commit: bool = True,
) -> str:
    """Record an operation and return its id.

    ``commit=False`` lets a higher-level mutation publish related durable
    rows in one SQLite transaction.
    """
    if state not in OP_STATES:
        msg = f"Invalid operation state {state!r}: must be one of {sorted(OP_STATES)}"
        raise ValueError(msg)
    serialized_request = json.dumps(dict(request_json or {}))
    if state == OP_PENDING:
        sweep_expired_proposals(conn, project_key, commit=commit)
        row = conn.execute(
            """SELECT COUNT(*) AS n,
                      COALESCE(SUM(
                          length(CAST(request_json AS BLOB))
                          + length(CAST(COALESCE(diff_text, '') AS BLOB))
                      ), 0) AS bytes
               FROM operations
               WHERE project_key = ? AND state = ?""",
            (project_key, OP_PENDING),
        ).fetchone()
        if int(row["n"]) >= MAX_PENDING_OPERATIONS_PER_PROJECT:
            raise ValidationError(
                "Too many pending operations for this project; apply or discard existing work",
                details={"max_pending": MAX_PENDING_OPERATIONS_PER_PROJECT},
            )
        requested_bytes = len(serialized_request.encode("utf-8")) + len(
            (diff_text or "").encode("utf-8")
        )
        pending_bytes = int(row["bytes"])
        if pending_bytes + requested_bytes > MAX_PENDING_OPERATION_BYTES_PER_PROJECT:
            raise ValidationError(
                "Pending operation content exceeds the project storage limit",
                details={
                    "pending_bytes": pending_bytes,
                    "requested_bytes": requested_bytes,
                    "max_bytes": MAX_PENDING_OPERATION_BYTES_PER_PROJECT,
                },
            )
    op_id = operation_id or new_audit_id()
    conn.execute(
        """INSERT INTO operations (id, project_key, operation_type, tool_name, target_path,
           source, request_json, base_sha256, after_sha256, diff_text, snapshot_id, state,
           created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            op_id,
            project_key,
            operation_type,
            tool_name,
            target_path,
            source,
            serialized_request,
            base_sha256,
            after_sha256,
            diff_text,
            snapshot_id,
            state,
            _now().isoformat(),
            expires_at,
        ),
    )
    if commit:
        conn.commit()
    return op_id


def record_proposal(
    conn: DbConnection,
    *,
    project_key: str,
    operation_type: str,
    target_path: str,
    request_json: JsonMapping,
    base_sha256: str | None,
    after_sha256: str,
    diff_text: str,
) -> tuple[str, str]:
    """Record a pending proposal and return ``(operation_id, expires_at)``."""
    if operation_type not in PROPOSAL_OP_TYPES:
        msg = f"{operation_type!r} is not a proposal operation type"
        raise ValueError(msg)
    expires_at = (_now() + PROPOSAL_TTL).isoformat()
    op_id = record_operation(
        conn,
        project_key=project_key,
        operation_type=operation_type,
        tool_name=operation_type,
        target_path=target_path,
        request_json=request_json,
        base_sha256=base_sha256,
        after_sha256=after_sha256,
        diff_text=diff_text,
        state=OP_PENDING,
        expires_at=expires_at,
        operation_id=new_proposal_id(),
    )
    return op_id, expires_at


def mark_operation_state(
    conn: DbConnection,
    operation_id: str,
    state: str,
    *,
    commit: bool = True,
) -> None:
    """Update the state of an existing operation (e.g. pending → applied)."""
    if state not in OP_STATES:
        msg = f"Invalid operation state {state!r}: must be one of {sorted(OP_STATES)}"
        raise ValueError(msg)
    conn.execute(
        """UPDATE operations
           SET state = ?,
               request_json = CASE
                   WHEN state = ? AND ? != ? THEN '{}'
                   ELSE request_json
               END,
               diff_text = CASE
                   WHEN state = ? AND ? != ? THEN NULL
                   ELSE diff_text
               END
           WHERE id = ?""",
        (
            state,
            OP_PENDING,
            state,
            OP_PENDING,
            OP_PENDING,
            state,
            OP_PENDING,
            operation_id,
        ),
    )
    if commit:
        conn.commit()


def get_operation(conn: DbConnection, operation_id: str) -> OperationRecord | None:
    """Fetch a single operation record by id."""
    row = conn.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
    if row is None:
        return None
    return _row_to_operation(row)


def is_expired(record: OperationRecord, *, now: datetime | None = None) -> bool:
    """Return whether a pending proposal is past its TTL."""
    if record.expires_at is None:
        if record.state == OP_PENDING:
            raise InvalidOperationError(
                "Pending operation has no expiration timestamp",
                details={"operation_id": record.id},
            )
        return False
    expires = _parse_expiration(record.id, record.expires_at)
    comparison_time = now or _now()
    if comparison_time.utcoffset() is None:
        raise InvalidOperationError("Expiration comparison time must include a timezone")
    return comparison_time > expires


def sweep_expired_proposals(
    conn: DbConnection,
    project_key: str,
    *,
    commit: bool = True,
) -> int:
    """Mark past-TTL pending proposals as expired; returns the count marked.

    ``commit=False`` preserves the transaction boundary owned by a
    higher-level mutation.
    """
    now = _now()
    rows = conn.execute(
        """SELECT id, expires_at FROM operations
           WHERE project_key = ? AND state = ?""",
        (project_key, OP_PENDING),
    ).fetchall()
    expired_ids: list[str] = []
    for row in rows:
        operation_id = str(row["id"])
        raw_expiration = row["expires_at"]
        if not isinstance(raw_expiration, str):
            raise InvalidOperationError(
                "Pending operation has no valid expiration timestamp",
                details={"operation_id": operation_id},
            )
        if now > _parse_expiration(operation_id, raw_expiration):
            expired_ids.append(operation_id)
    conn.executemany(
        """UPDATE operations
           SET state = ?, request_json = '{}', diff_text = NULL
           WHERE id = ? AND state = ?""",
        [(OP_EXPIRED, operation_id, OP_PENDING) for operation_id in expired_ids],
    )
    if commit:
        conn.commit()
    return len(expired_ids)


def _parse_expiration(operation_id: str, value: str) -> datetime:
    try:
        expires = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidOperationError(
            "Operation expiration timestamp is malformed",
            details={"operation_id": operation_id},
        ) from exc
    if expires.utcoffset() is None:
        raise InvalidOperationError(
            "Operation expiration timestamp must include a timezone",
            details={"operation_id": operation_id},
        )
    return expires


def mark_stale_proposals(
    conn: DbConnection,
    *,
    project_key: str,
    target_path: str,
    current_sha256: str | None,
) -> int:
    """Mark pending proposals for *target_path* whose base hash no longer matches.

    Called by reconcile-on-read after an out-of-band edit: any pending
    proposal bound to a different document hash than what is now on disk can
    no longer apply cleanly and is marked ``stale``. Returns the count marked.
    """
    params: list[object] = [OP_STALE, project_key, OP_PENDING, target_path]
    sql = """UPDATE operations SET state = ?, request_json = '{}', diff_text = NULL
             WHERE project_key = ? AND state = ? AND target_path = ?"""
    if current_sha256 is not None:
        sql += " AND (base_sha256 IS NULL OR base_sha256 != ?)"
        params.append(current_sha256)
    cursor = conn.execute(sql, tuple(params))
    conn.commit()
    return cursor.rowcount


def list_operations(
    conn: DbConnection,
    project_key: str,
    *,
    target_path: str | None = None,
    limit: int = 50,
) -> Sequence[OperationRecord]:
    """List recent operations for a project, newest first."""
    params: list[object] = [project_key]
    sql = "SELECT * FROM operations WHERE project_key = ?"
    if target_path is not None:
        sql += " AND target_path = ?"
        params.append(target_path)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_operation(row) for row in rows]


def list_pending_proposals(
    conn: DbConnection,
    project_key: str,
    *,
    target_path: str | None = None,
    limit: int = 50,
) -> Sequence[OperationRecord]:
    """List pending proposals for a project (after sweeping expired ones)."""
    sweep_expired_proposals(conn, project_key)
    placeholders = ",".join("?" for _ in PROPOSAL_OP_TYPES)
    params: list[object] = [project_key, OP_PENDING, *sorted(PROPOSAL_OP_TYPES)]
    # S608: placeholders is generated solely from the closed
    # PROPOSAL_OP_TYPES constant; every value remains a bound parameter.
    sql = (
        f"SELECT * FROM operations WHERE project_key = ? AND state = ? "  # noqa: S608
        f"AND operation_type IN ({placeholders})"
    )
    if target_path is not None:
        sql += " AND target_path = ?"
        params.append(target_path)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_operation(row) for row in rows]


def find_equivalent_pending_proposal(
    conn: DbConnection,
    *,
    project_key: str,
    operation_type: str,
    target_path: str,
    base_sha256: str | None,
    target: JsonObject,
) -> OperationRecord | None:
    """Return an existing pending proposal with the same edit intent, if any.

    The dedupe key is ``state=pending`` plus identical project, type, path,
    base document hash, and target descriptor — the *complete intent* of the
    edit, including a replacement/insert value or its stable hash.
    The prepared content itself cannot participate: every preparation
    refreshes the ``updated`` frontmatter timestamp, so byte-identical
    retries still produce distinct content. Because ``base_sha256``
    participates, a change to the underlying file naturally prevents reusing
    a stale proposal.
    """
    rows = conn.execute(
        """SELECT * FROM operations
           WHERE state = ?
             AND project_key = ?
             AND operation_type = ?
             AND target_path = ?
             AND base_sha256 IS ?
           ORDER BY created_at DESC""",
        (OP_PENDING, project_key, operation_type, target_path, base_sha256),
    ).fetchall()
    for row in rows:
        record = _row_to_operation(row)
        if record.request_json.get("target") == target:
            return record
    return None


def _row_to_operation(row: DbRow) -> OperationRecord:
    request_json: JsonObject = {}
    if row["request_json"]:
        try:
            decoded = json.loads(row["request_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise InvalidOperationError(
                "Operation request metadata is malformed",
                details={"operation_id": str(row["id"])},
            ) from exc
        payload = _coerce_json_object(decoded)
        if payload is None:
            raise InvalidOperationError(
                "Operation request metadata must be a JSON object",
                details={"operation_id": str(row["id"])},
            )
        request_json = payload
    return OperationRecord(
        id=row["id"],
        project_key=row["project_key"],
        operation_type=row["operation_type"],
        tool_name=row["tool_name"],
        target_path=row["target_path"],
        source=row["source"],
        request_json=request_json,
        base_sha256=row["base_sha256"],
        after_sha256=row["after_sha256"],
        diff_text=row["diff_text"],
        snapshot_id=row["snapshot_id"],
        state=row["state"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _coerce_json_object(value: object) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    raw_dict = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in raw_dict):
        return None
    return cast(JsonObject, value)
