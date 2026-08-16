"""Transport deliverability on the read surfaces (S-10).

An oversized reply is not a slow reply. The relay rejects a body over the
configured ceiling with HTTP 413 and that rejection kills the stdio child, so
these surfaces have to fail *before* they emit rather than after. The tests
use tiny ceilings rather than 10 MiB fixtures: the guard is about the
relationship between a measured size and a configured limit, and nothing in it
cares which side of that relationship was made small.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ferumind.core.context import build_context
from ferumind.core.document_map import read_document_range
from ferumind.core.errors import ResponseTooLargeError, SnapshotNotFoundError
from ferumind.core.paths import WorkspaceRoot, contained_path, contained_project_root
from ferumind.core.reads import (
    MAX_SNAPSHOT_TEXT_BYTES,
    read_project_document,
    read_project_snapshot,
    snapshot_side_fits,  # internal helper: the budget/own-cap interaction has no public surface
)
from ferumind.core.registry import require_project
from ferumind.core.response_limits import ResponseBudget, charged_text_bytes
from ferumind.core.snapshots import create_snapshot, new_snapshot_id
from tests.conftest import managed_markdown


def _write_document(workspace: WorkspaceRoot, project: str, path: str, body: str) -> None:
    target = contained_path(contained_project_root(workspace, project), path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(managed_markdown(body), encoding="utf-8")


# ── The budget itself ────────────────────────────────────────────────────────


def test_budget_without_a_limit_never_refuses() -> None:
    """In-process callers — CLI, dashboard, tests — cross no transport."""
    budget = ResponseBudget(None, surface="test")

    budget.charge(50 * 1024 * 1024, source="anything", remedy="none needed")

    assert budget.remaining_bytes is None


def test_budget_charges_a_json_allowance_over_the_measured_size() -> None:
    """The estimate must run over the wire size, never under it.

    An early refusal that trips after the transport already rejected the
    body is the failure this whole mechanism exists to prevent.
    """
    assert charged_text_bytes(1600) > 1600

    budget = ResponseBudget(1600, surface="test")

    assert budget.try_charge(1600) is False
    assert budget.try_charge(1500) is True


def test_budget_error_names_the_contributor_and_the_way_out() -> None:
    budget = ResponseBudget(1024, surface="get_context")
    budget.charge(512, source="the first file", remedy="irrelevant")

    with pytest.raises(ResponseTooLargeError) as excinfo:
        budget.charge(4096, source="the rules document rules/big.md", remedy="Split it.")

    error = excinfo.value
    assert error.code == "RESPONSE_TOO_LARGE"
    assert error.details is not None
    assert error.details["source"] == "the rules document rules/big.md"
    assert error.details["source_bytes"] == 4096
    assert error.details["max_response_bytes"] == 1024
    assert error.details["recommended_action"] == "Split it."
    # The rejected charge is not banked: a caller that catches this and asks
    # for something smaller must not be billed for what it never got.
    assert budget.used_bytes == charged_text_bytes(512)


# ── read_document ────────────────────────────────────────────────────────────


def test_read_document_refuses_a_body_the_transport_cannot_carry(
    workspace: WorkspaceRoot, project: str
) -> None:
    _write_document(workspace, project, "canvases/big.md", "x" * 4096)

    with pytest.raises(ResponseTooLargeError) as excinfo:
        read_project_document(workspace, project, "canvases/big.md", max_response_bytes=1024)

    details = excinfo.value.details
    assert details is not None
    assert details["surface"] == "read_document"
    assert details["source"] == "the document canvases/big.md"


def test_read_document_serves_the_whole_body_when_it_fits(
    workspace: WorkspaceRoot, project: str
) -> None:
    """The guard refuses; it never truncates.

    ``document_sha256`` has to describe the whole file for hash-guarded
    edits to be safe, so a partial body would be worse than an error.
    """
    _write_document(workspace, project, "canvases/small.md", "x" * 64)

    document = read_project_document(
        workspace, project, "canvases/small.md", max_response_bytes=1024 * 1024
    )

    assert "x" * 64 in document.content


def test_oversized_document_stays_readable_through_the_bounded_surfaces(
    workspace: WorkspaceRoot, project: str
) -> None:
    """Refusing to *serve* a document whole is not refusing to serve it.

    The range, map, and find tools build small results from the same read
    and pass no ceiling. If this ever fails, a single oversized file has
    made itself unreachable by every means instead of one.
    """
    _write_document(workspace, project, "canvases/big.md", "line\n" * 2000)

    document = read_project_document(workspace, project, "canvases/big.md")
    result = read_document_range(
        content=document.content,
        project_key=project,
        path=document.path,
        start_line=1,
        end_line=3,
    )

    assert result.range.end_line == 3


# ── get_context ──────────────────────────────────────────────────────────────


def _context(conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str, limit: int | None):
    return build_context(
        conn, workspace, require_project(workspace, project), max_response_bytes=limit
    )


def test_get_context_refuses_and_names_the_rules_document(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    """The bootstrap's mandated first call must fail loudly, not fatally.

    Past the ceiling every chat in the project dies on its first call and
    takes the transport with it. An error naming the file is the difference
    between an operator who splits it and a bug report about a connection
    that keeps dropping.
    """
    _write_document(workspace, project, "rules/big.md", "x" * 200_000)

    with pytest.raises(ResponseTooLargeError) as excinfo:
        _context(conn, workspace, project, 100_000)

    details = excinfo.value.details
    assert details is not None
    assert details["surface"] == "get_context"
    assert details["source"] == f"the rules document projects/{project}/rules/big.md"


def test_get_context_refuses_an_oversized_spine(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    _write_document(workspace, project, "spine.md", "x" * 200_000)

    with pytest.raises(ResponseTooLargeError) as excinfo:
        _context(conn, workspace, project, 100_000)

    details = excinfo.value.details
    assert details is not None
    assert details["source"] == "the spine spine.md"


def test_get_context_stays_uncapped_below_the_ceiling(
    conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
) -> None:
    """Uncapped is a locked product decision (spec-mcp §4) and still holds.

    A deliverable payload is assembled exactly as before: every rules file
    present, nothing truncated, nothing paged.
    """
    unbounded = _context(conn, workspace, project, None)
    bounded = _context(conn, workspace, project, 10 * 1024 * 1024)

    assert bounded.rules.sources == unbounded.rules.sources
    assert bounded.rules.content_markdown == unbounded.rules.content_markdown
    assert bounded.payload.rules_bytes == unbounded.payload.rules_bytes


# ── read_snapshot ────────────────────────────────────────────────────────────


def _snapshot(
    workspace: WorkspaceRoot,
    project: str,
    *,
    before: str | None,
    after: str | None,
) -> tuple[str, Path]:
    snapshot_id = new_snapshot_id()
    snapshot_dir = create_snapshot(
        contained_project_root(workspace, project),
        project_key=project,
        target_path="canvases/a.md",
        before_content=before,
        after_content=after,
        reason="test",
        snapshot_id=snapshot_id,
    )
    return snapshot_id, snapshot_dir


def test_snapshot_omits_components_rather_than_refusing_the_read(
    workspace: WorkspaceRoot, project: str
) -> None:
    """Three components, one transport, and an omission contract that predates it.

    ``*_omitted`` already existed for binary and over-cap content, so a
    snapshot degrades in the way its own result schema documents. Priority
    is diff, then before, then after.
    """
    snapshot_id, snapshot_dir = _snapshot(
        workspace, project, before="old\n" * 500, after="new\n" * 500
    )
    # Room for the diff and one side, and provably not for the second side.
    side_bytes = charged_text_bytes(len(("old\n" * 500).encode()))
    diff_bytes = charged_text_bytes((snapshot_dir / "diff.patch").stat().st_size)
    limit = diff_bytes + side_bytes + 1

    result = read_project_snapshot(workspace, project, snapshot_id, max_response_bytes=limit)

    assert result.diff_omitted is False
    assert result.diff != ""
    assert result.before_content is not None
    assert result.before_content_omitted is False
    assert result.after_content is None
    assert result.after_content_omitted is True


def test_snapshot_omitted_for_budget_is_still_integrity_checked(
    workspace: WorkspaceRoot, project: str
) -> None:
    """Suppressing a body must not suppress its verification.

    A corrupt snapshot has to be caught on the read that happens not to
    return the corrupt side, or the guard has quietly turned a size limit
    into a way to hide tampering.
    """
    snapshot_id, snapshot_dir = _snapshot(
        workspace, project, before="old\n" * 500, after="new\n" * 500
    )
    stored_after = snapshot_dir / "after" / "canvases" / "a.md"
    stored_after.write_bytes(b"z" * stored_after.stat().st_size)
    # A budget that leaves nothing for either side: both are omitted, and
    # the corruption still has to surface.
    diff_bytes = charged_text_bytes((snapshot_dir / "diff.patch").stat().st_size)

    with pytest.raises(SnapshotNotFoundError):
        read_project_snapshot(workspace, project, snapshot_id, max_response_bytes=diff_bytes)


def test_snapshot_side_over_its_own_cap_does_not_consume_the_budget(
    workspace: WorkspaceRoot, project: str
) -> None:
    """A side omitted by its own 5 MiB cap was never going on the wire.

    Charging for it would spend the response budget on bytes nobody
    receives, and starve the components that could have been served.
    """
    budget = ResponseBudget(1024, surface="read_snapshot")

    assert snapshot_side_fits(MAX_SNAPSHOT_TEXT_BYTES + 1, budget) is False
    assert budget.used_bytes == 0
    assert snapshot_side_fits(None, budget) is False
    assert budget.used_bytes == 0


def test_snapshot_without_a_limit_is_unchanged(workspace: WorkspaceRoot, project: str) -> None:
    snapshot_id, _dir = _snapshot(workspace, project, before="old\n", after="new\n")

    result = read_project_snapshot(workspace, project, snapshot_id)

    assert result.before_content == "old\n"
    assert result.after_content == "new\n"
    assert result.diff_omitted is False
