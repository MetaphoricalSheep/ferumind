"""Tests for folder = role derivation (spec-mcp §2)."""

from __future__ import annotations

import pytest

from lattice.core.errors import UnknownFolderError
from lattice.core.folders import (
    archive_path_for,
    default_edit_policy,
    folder_of,
    is_archived_path,
    origin_path_for,
)


@pytest.mark.parametrize(
    ("path", "folder"),
    [
        ("spine.md", "spine"),
        ("rules/00-project.md", "rules"),
        ("canvases/plan.md", "canvases"),
        ("canvases/2026/07/log.md", "canvases"),
        ("memory/notes.md", "memory"),
        ("library/runbooks/rebuild.md", "library"),
        ("inbox/capture.md", "inbox"),
        ("archive/canvases/old.md", "archive"),
    ],
)
def test_folder_of_derives_role_from_first_segment(path: str, folder: str) -> None:
    assert folder_of(path) == folder


@pytest.mark.parametrize("path", ["", "notes.md", "unknown/doc.md", "docs/readme.md"])
def test_folder_of_rejects_paths_outside_the_layout(path: str) -> None:
    with pytest.raises(UnknownFolderError):
        folder_of(path)


@pytest.mark.parametrize(
    ("folder", "policy"),
    [
        ("spine", "propose-first"),
        ("rules", "ask-human"),
        ("canvases", "free"),
        ("memory", "free"),
        ("inbox", "free"),
        ("library", "propose-first"),
    ],
)
def test_default_edit_policy_by_folder(folder: str, policy: str) -> None:
    assert default_edit_policy(folder) == policy  # type: ignore[arg-type] - literal narrowing in test params


def test_archive_path_mirrors_origin() -> None:
    assert archive_path_for("canvases/plan.md") == "archive/canvases/plan.md"
    assert origin_path_for("archive/canvases/plan.md") == "canvases/plan.md"


def test_origin_path_rejects_non_archive_paths() -> None:
    with pytest.raises(UnknownFolderError):
        origin_path_for("canvases/plan.md")


def test_is_archived_path() -> None:
    assert is_archived_path("archive/memory/x.md")
    assert not is_archived_path("memory/x.md")
