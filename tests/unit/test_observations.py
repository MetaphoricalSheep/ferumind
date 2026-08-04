"""Tests for the metadata-only MCP observation log."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import cast

from mcp.types import CallToolResult, TextContent

from lattice.core.observations import (
    get_observation,
    list_observations,
    new_correlation_id,
    record_mcp_call_observation,
)
from lattice.mcp import observation as observation_module
from lattice.mcp.models import make_success

# This focused test deliberately exercises an internal size helper. Resolve it
# dynamically so strict typing does not mistake the test-only access for a
# supported module export.
_result_bytes = cast(
    Callable[[CallToolResult], int],
    vars(observation_module)["_result_bytes"],
)


def test_record_and_list_observations(conn: sqlite3.Connection) -> None:
    obs_id = record_mcp_call_observation(
        conn,
        tool_name="get_context",
        project_key="demo",
        ok=True,
        transport="stdio",
        argument_keys=["project"],
        context_metrics={"rules_bytes": 100, "spine_bytes": 50, "documents_count": 3},
        duration_ms=12.5,
        result_bytes=2048,
    )
    record = get_observation(conn, obs_id)
    assert record is not None
    assert record.tool_name == "get_context"
    assert record.ok is True
    assert record.duration_ms == 12.5
    assert record.result_bytes == 2048
    assert record.correlation_id.startswith("lat_corr_")
    metrics = json.loads(record.context_metrics_json)
    assert metrics == {"rules_bytes": 100, "spine_bytes": 50, "documents_count": 3}
    assert json.loads(record.argument_keys_json) == ["project"]

    assert [o.id for o in list_observations(conn, tool_name="get_context")] == [obs_id]
    assert list_observations(conn, project_key="other") == []


def test_secrets_are_redacted(conn: sqlite3.Connection) -> None:
    obs_id = record_mcp_call_observation(
        conn,
        tool_name="x",
        context_metrics={"authorization": "Bearer secret", "harmless": 1},
    )
    record = get_observation(conn, obs_id)
    assert record is not None
    metrics = json.loads(record.context_metrics_json)
    assert metrics["authorization"] == "[redacted]"
    assert metrics["harmless"] == 1
    notes = json.loads(record.redaction_notes_json)
    assert any("authorization" in note for note in notes)
    assert "secret" not in record.context_metrics_json


def test_oversized_metadata_is_truncated(conn: sqlite3.Connection) -> None:
    obs_id = record_mcp_call_observation(
        conn,
        tool_name="x",
        argument_keys=[f"key_{i}" for i in range(2000)],
    )
    record = get_observation(conn, obs_id)
    assert record is not None
    assert len(record.argument_keys_json.encode("utf-8")) <= 4 * 1024
    assert json.loads(record.argument_keys_json)["__truncated__"] is True


def test_error_calls_record_code(conn: sqlite3.Connection) -> None:
    obs_id = record_mcp_call_observation(
        conn, tool_name="apply_patch", ok=False, error_code="PATCH_CONFLICT"
    )
    record = get_observation(conn, obs_id)
    assert record is not None
    assert record.ok is False
    assert record.error_code == "PATCH_CONFLICT"


def test_correlation_ids_unique() -> None:
    assert new_correlation_id() != new_correlation_id()


def test_result_size_counts_the_full_serialized_mcp_result() -> None:
    result = make_success({"payload": "é"})
    expected = len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))
    text_only = sum(
        len(item.text.encode("utf-8")) for item in result.content if isinstance(item, TextContent)
    )

    assert _result_bytes(result) == expected
    assert expected > text_only
