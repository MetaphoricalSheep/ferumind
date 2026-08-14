"""Tests for the metadata-only MCP observation log."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import cast

from mcp.types import TextContent

from ferumind.core.observations import (
    SERVER_BOOT_ID,
    get_observation,
    get_observation_by_correlation_id,
    list_observations,
    new_correlation_id,
    record_mcp_call_observation,
)
from ferumind.mcp import observation as observation_module
from ferumind.mcp.models import make_success

# This focused test deliberately exercises an internal size helper. Resolve it
# dynamically so strict typing does not mistake the test-only access for a
# supported module export.
_serialized_bytes = cast(
    Callable[[object], int | None],
    vars(observation_module)["_serialized_bytes"],
)


def test_record_and_list_observations(conn: sqlite3.Connection) -> None:
    obs_id = record_mcp_call_observation(
        conn,
        tool_name="get_context",
        project_key="demo",
        ok=True,
        transport="stdio",
        argument_keys=["project"],
        context_metrics={
            "rules_bytes": 100,
            "spine_bytes": 50,
            "documents_count": 3,
            "descriptions_bytes": 75,
        },
        duration_ms=12.5,
        result_bytes=2048,
    )
    record = get_observation(conn, obs_id)
    assert record is not None
    assert record.tool_name == "get_context"
    assert record.ok is True
    assert record.duration_ms == 12.5
    assert record.result_bytes == 2048
    assert record.correlation_id.startswith("fm_corr_")
    metrics = json.loads(record.context_metrics_json)
    assert metrics == {
        "rules_bytes": 100,
        "spine_bytes": 50,
        "documents_count": 3,
        "descriptions_bytes": 75,
    }
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


def test_observation_is_queryable_by_exact_opaque_correlation_id(
    conn: sqlite3.Connection,
) -> None:
    record_mcp_call_observation(
        conn,
        tool_name="get_context",
        correlation_id="fm_corr_incident",
    )
    current = get_observation_by_correlation_id(conn, "fm_corr_incident")
    assert current is not None
    assert current.tool_name == "get_context"
    assert get_observation_by_correlation_id(conn, "missing") is None


def test_no_new_identifier_carries_the_former_project_prefix(
    conn: sqlite3.Connection,
) -> None:
    """All three generated id kinds, not just the one a spot-check would catch.

    The boot id is generated once at import, so it is asserted on the module
    constant rather than on a row this test could have produced.
    """
    obs_id = record_mcp_call_observation(conn, tool_name="x")
    record = get_observation(conn, obs_id)
    assert record is not None
    generated = (obs_id, record.correlation_id, record.server_boot_id, SERVER_BOOT_ID)
    assert not any(value.startswith("lat_") for value in generated), generated
    assert obs_id.startswith("fm_obs_")
    assert record.correlation_id.startswith("fm_corr_")
    assert SERVER_BOOT_ID.startswith("fm_boot_")


def test_identifiers_written_under_the_former_prefix_stay_readable(
    conn: sqlite3.Connection,
) -> None:
    """The live database holds thousands of `lat_*` rows; none were migrated.

    A reader that assumed either prefix would silently drop half the log, so
    this inserts a row in the historical form and reads it back through the
    same accessors the new form uses.
    """
    conn.execute(
        """INSERT INTO mcp_call_observations
           (id, correlation_id, tool_name, project_key, created_at,
            ok, server_boot_id, process_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "lat_obs_0123456789abcdef",
            "lat_corr_fedcba9876543210",
            "get_context",
            "demo",
            "2026-01-01T00:00:00+00:00",
            1,
            "lat_boot_legacy",
            1234,
        ),
    )
    conn.commit()

    record = get_observation(conn, "lat_obs_0123456789abcdef")
    assert record is not None
    assert record.correlation_id == "lat_corr_fedcba9876543210"
    assert record.server_boot_id == "lat_boot_legacy"

    listed = list_observations(conn, tool_name="get_context")
    assert "lat_obs_0123456789abcdef" in [o.id for o in listed]

    by_correlation = get_observation_by_correlation_id(conn, "lat_corr_fedcba9876543210")
    assert by_correlation is not None
    assert by_correlation.id == "lat_obs_0123456789abcdef"


def test_result_size_counts_the_full_serialized_mcp_result() -> None:
    """Byte counts come from the wire form the middleware is handed.

    ``call_next`` returns the already-serialized result dict, so this measures
    exactly what the transport writes — including ``structuredContent``, not
    just the text blocks.
    """
    result = make_success({"payload": "é"})
    wire = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    text_only = sum(
        len(item.text.encode("utf-8")) for item in result.content if isinstance(item, TextContent)
    )

    measured = _serialized_bytes(wire)
    assert measured is not None
    assert measured == len(json.dumps(wire, separators=(",", ":")).encode("utf-8"))
    assert measured > text_only


def test_result_size_is_none_for_an_unserializable_result() -> None:
    """Telemetry degrades to a null column rather than raising into a call."""
    assert _serialized_bytes({"bad": {1, 2, 3}}) is None
