"""Tests for the operation log and pending-proposal lifecycle."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from ferumind.core import operations as operations_module
from ferumind.core.errors import InvalidOperationError, ValidationError
from ferumind.core.operations import (
    OP_DISCARDED,
    OP_EXPIRED,
    OP_PENDING,
    OP_STALE,
    find_equivalent_pending_proposal,
    get_operation,
    is_expired,
    list_operations,
    list_pending_proposals,
    mark_operation_state,
    mark_stale_proposals,
    new_proposal_id,
    record_operation,
    record_proposal,
    sweep_expired_proposals,
)
from ferumind.core.types import JsonObject


def _proposal(conn: sqlite3.Connection, project: str = "demo", path: str = "canvases/a.md") -> str:
    op_id, expires_at = record_proposal(
        conn,
        project_key=project,
        operation_type="propose_exact_replace_patch",
        target_path=path,
        request_json={
            "new_content": "x",
            "target": {"kind": "exact_replace", "old_string": "a", "new_string": "b"},
        },
        base_sha256="base123",
        after_sha256="after456",
        diff_text="--- diff ---",
    )
    assert expires_at
    return op_id


def test_proposal_ids_are_unguessable_and_prefixed(conn: sqlite3.Connection) -> None:
    op_id = new_proposal_id()
    assert op_id.startswith("op_")
    # token_urlsafe(24) → 32 chars of entropy after the prefix (≥128-bit random).
    assert len(op_id) >= 3 + 32
    assert new_proposal_id() != op_id


def test_record_proposal_sets_pending_state_and_ttl(conn: sqlite3.Connection) -> None:
    op_id = _proposal(conn)
    op = get_operation(conn, op_id)
    assert op is not None
    assert op.state == OP_PENDING
    assert op.expires_at is not None
    expires = datetime.fromisoformat(op.expires_at)
    delta = expires - datetime.now(UTC)
    assert timedelta(hours=23) < delta <= timedelta(hours=24)
    assert not is_expired(op)


def test_record_proposal_rejects_non_proposal_types(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="not a proposal"):
        record_proposal(
            conn,
            project_key="demo",
            operation_type="apply_patch",
            target_path="canvases/a.md",
            request_json={},
            base_sha256=None,
            after_sha256="x",
            diff_text="",
        )


def test_sweep_marks_past_ttl_proposals_expired(conn: sqlite3.Connection) -> None:
    op_id = _proposal(conn)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    conn.execute("UPDATE operations SET expires_at = ? WHERE id = ?", (past, op_id))
    conn.commit()
    assert sweep_expired_proposals(conn, "demo") == 1
    op = get_operation(conn, op_id)
    assert op is not None
    assert op.state == OP_EXPIRED
    assert op.request_json == {}
    assert op.diff_text is None
    assert list_pending_proposals(conn, "demo") == []


def test_pending_record_with_commit_false_does_not_commit_expiration_sweep(
    conn: sqlite3.Connection,
) -> None:
    expired_id = _proposal(conn)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    conn.execute("UPDATE operations SET expires_at = ? WHERE id = ?", (past, expired_id))
    conn.commit()

    record_operation(
        conn,
        project_key="demo",
        operation_type="transaction_sentinel",
        operation_id="transaction-sentinel",
        commit=False,
    )
    record_operation(
        conn,
        project_key="demo",
        operation_type="upload_file_session",
        state=OP_PENDING,
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        operation_id="transaction-pending",
        commit=False,
    )
    conn.rollback()

    assert get_operation(conn, "transaction-sentinel") is None
    assert get_operation(conn, "transaction-pending") is None
    expired = get_operation(conn, expired_id)
    assert expired is not None
    assert expired.state == OP_PENDING


def test_malformed_operation_request_json_fails_closed(conn: sqlite3.Connection) -> None:
    op_id = _proposal(conn)
    conn.execute("UPDATE operations SET request_json = ? WHERE id = ?", ("{broken", op_id))
    conn.commit()

    with pytest.raises(InvalidOperationError) as excinfo:
        get_operation(conn, op_id)
    assert excinfo.value.code == "INVALID_OPERATION"
    assert excinfo.value.details == {"operation_id": op_id}


@pytest.mark.parametrize("expires_at", ["not-an-iso-timestamp", "2030-01-01T00:00:00", None])
def test_malformed_pending_expiration_fails_closed(
    conn: sqlite3.Connection, expires_at: str | None
) -> None:
    op_id = _proposal(conn)
    conn.execute("UPDATE operations SET expires_at = ? WHERE id = ?", (expires_at, op_id))
    conn.commit()
    operation = get_operation(conn, op_id)
    assert operation is not None

    with pytest.raises(InvalidOperationError):
        is_expired(operation)
    with pytest.raises(InvalidOperationError):
        sweep_expired_proposals(conn, "demo")


def test_mark_stale_only_hits_mismatched_hashes(conn: sqlite3.Connection) -> None:
    matching = _proposal(conn)
    conn.execute("UPDATE operations SET base_sha256 = 'current' WHERE id = ?", (matching,))
    conn.commit()
    mismatched = _proposal(conn)
    staled = mark_stale_proposals(
        conn, project_key="demo", target_path="canvases/a.md", current_sha256="current"
    )
    assert staled == 1
    matched_op = get_operation(conn, matching)
    mismatched_op = get_operation(conn, mismatched)
    assert matched_op is not None
    assert matched_op.state == OP_PENDING
    assert mismatched_op is not None
    assert mismatched_op.state == OP_STALE


def test_mark_stale_without_hash_stales_everything(conn: sqlite3.Connection) -> None:
    _proposal(conn)
    _proposal(conn, path="canvases/b.md")
    staled = mark_stale_proposals(
        conn, project_key="demo", target_path="canvases/a.md", current_sha256=None
    )
    assert staled == 1


def test_find_equivalent_pending_proposal_matches_intent(conn: sqlite3.Connection) -> None:
    op_id = _proposal(conn)
    target: JsonObject = {"kind": "exact_replace", "old_string": "a", "new_string": "b"}
    found = find_equivalent_pending_proposal(
        conn,
        project_key="demo",
        operation_type="propose_exact_replace_patch",
        target_path="canvases/a.md",
        base_sha256="base123",
        target=target,
    )
    assert found is not None
    assert found.id == op_id
    # A different base hash never matches.
    assert (
        find_equivalent_pending_proposal(
            conn,
            project_key="demo",
            operation_type="propose_exact_replace_patch",
            target_path="canvases/a.md",
            base_sha256="other",
            target=target,
        )
        is None
    )
    # A different edit intent never matches.
    assert (
        find_equivalent_pending_proposal(
            conn,
            project_key="demo",
            operation_type="propose_exact_replace_patch",
            target_path="canvases/a.md",
            base_sha256="base123",
            target={"kind": "exact_replace", "old_string": "a", "new_string": "OTHER"},
        )
        is None
    )


def test_list_operations_filters_by_path(conn: sqlite3.Connection) -> None:
    record_operation(conn, project_key="demo", operation_type="create_document", target_path="a.md")
    record_operation(conn, project_key="demo", operation_type="create_document", target_path="b.md")
    assert len(list_operations(conn, "demo")) == 2
    assert len(list_operations(conn, "demo", target_path="a.md")) == 1


def test_mark_operation_state_validates(conn: sqlite3.Connection) -> None:
    op_id = _proposal(conn)
    with pytest.raises(ValueError, match="Invalid operation state"):
        mark_operation_state(conn, op_id, "bogus")
    with pytest.raises(ValueError, match="Invalid operation state"):
        record_operation(conn, project_key="demo", operation_type="x", state="bogus")


def test_terminal_state_scrubs_pending_content(conn: sqlite3.Connection) -> None:
    op_id = _proposal(conn)
    mark_operation_state(conn, op_id, OP_DISCARDED)

    operation = get_operation(conn, op_id)
    assert operation is not None
    assert operation.state == OP_DISCARDED
    assert operation.request_json == {}
    assert operation.diff_text is None


def test_pending_content_has_a_project_byte_quota(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(operations_module, "MAX_PENDING_OPERATION_BYTES_PER_PROJECT", 1)

    with pytest.raises(ValidationError, match="storage limit"):
        _proposal(conn)
