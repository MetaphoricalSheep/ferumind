"""Adversarial tests for bounded, integrity-checked core reads."""

from __future__ import annotations

from pathlib import Path

import pytest

from ferumind.core import reads
from ferumind.core.errors import SnapshotNotFoundError
from ferumind.core.paths import WorkspaceRoot, contained_project_root
from ferumind.core.reads import (
    MAX_SNAPSHOT_TEXT_BYTES,
    read_project_snapshot,
)
from ferumind.core.snapshots import create_snapshot, new_snapshot_id


def _create_project_snapshot(
    workspace: WorkspaceRoot,
    project: str,
    *,
    before: str | None = "old\n",
    after: str | None = "new\n",
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


def test_project_snapshot_read_verifies_and_returns_both_sides(
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    snapshot_id, _snapshot_dir = _create_project_snapshot(workspace, project)

    result = read_project_snapshot(workspace, project, snapshot_id)

    assert result.before_content == "old\n"
    assert result.after_content == "new\n"
    assert result.before_content_omitted is False
    assert result.after_content_omitted is False
    assert result.diff_omitted is False


@pytest.mark.parametrize(
    ("side", "substitute"),
    [
        ("before", b"bad\n"),  # same size, different digest
        ("after", b"expanded\n"),  # different stored size
    ],
)
def test_project_snapshot_read_rejects_corrupted_sides(
    workspace: WorkspaceRoot,
    project: str,
    side: str,
    substitute: bytes,
) -> None:
    snapshot_id, snapshot_dir = _create_project_snapshot(workspace, project)
    (snapshot_dir / side / "canvases/a.md").write_bytes(substitute)

    with pytest.raises(SnapshotNotFoundError, match="corrupted"):
        read_project_snapshot(workspace, project, snapshot_id)


def test_project_snapshot_read_rejects_unexpected_side_without_metadata(
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    snapshot_id, snapshot_dir = _create_project_snapshot(
        workspace,
        project,
        before=None,
    )
    unexpected = snapshot_dir / "before/canvases/a.md"
    unexpected.parent.mkdir(parents=True)
    unexpected.write_text("injected\n", encoding="utf-8")

    with pytest.raises(SnapshotNotFoundError, match="before"):
        read_project_snapshot(workspace, project, snapshot_id)


def test_project_snapshot_read_rejects_missing_declared_side(
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    snapshot_id, snapshot_dir = _create_project_snapshot(workspace, project)
    (snapshot_dir / "after/canvases/a.md").unlink()

    with pytest.raises(SnapshotNotFoundError, match="after"):
        read_project_snapshot(workspace, project, snapshot_id)


def test_project_snapshot_read_enforces_stored_file_safety_cap(
    monkeypatch: pytest.MonkeyPatch,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    snapshot_id, _snapshot_dir = _create_project_snapshot(workspace, project)
    monkeypatch.setattr(reads, "MAX_SNAPSHOT_STORED_FILE_BYTES", 3)

    with pytest.raises(SnapshotNotFoundError, match="before"):
        read_project_snapshot(workspace, project, snapshot_id)


def test_project_snapshot_read_bounds_but_still_verifies_large_content_and_diff(
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    large_content = "x" * (MAX_SNAPSHOT_TEXT_BYTES + 1)
    snapshot_id, snapshot_dir = _create_project_snapshot(
        workspace,
        project,
        before=None,
        after=large_content,
    )

    result = read_project_snapshot(workspace, project, snapshot_id)

    assert result.after_content is None
    assert result.after_content_omitted is True
    assert result.diff == ""
    assert result.diff_omitted is True

    after_file = snapshot_dir / "after/canvases/a.md"
    with after_file.open("r+b") as stream:
        stream.write(b"y")

    with pytest.raises(SnapshotNotFoundError, match="after"):
        read_project_snapshot(workspace, project, snapshot_id)
