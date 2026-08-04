"""Tests for the projects.yml registry and project scoping."""

from __future__ import annotations

import pytest

from lattice.core import registry as registry_core
from lattice.core.errors import ProjectNotFoundError, ProjectRequiredError, ValidationError
from lattice.core.paths import WorkspaceRoot
from lattice.core.registry import (
    ProjectEntry,
    add_registry_entry,
    list_entries,
    load_registry,
    registry_path,
    require_project,
    save_registry,
    serialize_registry,
    validate_project_key,
)


def test_validate_project_key() -> None:
    assert str(validate_project_key("garden-v2")) == "garden-v2"
    for bad in ("", "Garden", "2fast", "has space", "under_score", "demo\n"):
        with pytest.raises(ValidationError):
            validate_project_key(bad)


def test_registry_round_trip(workspace: WorkspaceRoot) -> None:
    assert load_registry(workspace) == {}
    entry = add_registry_entry(workspace, validate_project_key("demo"), "Demo")
    assert entry.key == "demo"
    loaded = load_registry(workspace)
    assert loaded["demo"].title == "Demo"
    assert loaded["demo"].path == "projects/demo"
    assert loaded["demo"].status == "active"
    assert [e.key for e in list_entries(workspace)] == ["demo"]


def test_load_registry_rejects_malformed_entries(workspace: WorkspaceRoot) -> None:
    registry_path(workspace).write_text(
        "projects:\n  BadKey:\n    title: nope\n  good:\n    title: Good\n  broken: notadict\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_registry(workspace)


def test_load_registry_rejects_control_characters_in_title(workspace: WorkspaceRoot) -> None:
    registry_path(workspace).write_text(
        'projects:\n  demo:\n    title: "bad\\ntitle"\n'
        "    path: projects/demo\n"
        "    status: active\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_registry(workspace)


def test_load_registry_rejects_garbage_yaml(workspace: WorkspaceRoot) -> None:
    registry_path(workspace).write_text("{ not yaml", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_registry(workspace)


def test_load_registry_rejects_oversized_file_before_parsing(
    workspace: WorkspaceRoot, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path(workspace).write_text("projects: {}\n" + ("#" * 100), encoding="utf-8")
    monkeypatch.setattr(registry_core, "MAX_REGISTRY_BYTES", 32)
    with pytest.raises(ValidationError, match="byte limit"):
        load_registry(workspace)


def test_save_registry_size_failure_preserves_existing_registry(
    workspace: WorkspaceRoot, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_registry_entry(workspace, validate_project_key("demo"), "Demo")
    path = registry_path(workspace)
    original = path.read_bytes()
    monkeypatch.setattr(registry_core, "MAX_REGISTRY_BYTES", 32)
    oversized = {
        "demo": ProjectEntry(
            key="demo",
            title="A title that cannot fit in the patched registry size limit",
            path="projects/demo",
            status="active",
        )
    }

    with pytest.raises(ValidationError, match="Serialized"):
        save_registry(workspace, oversized)
    assert path.read_bytes() == original


def test_serialize_registry_enforces_project_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry_core, "MAX_REGISTRY_PROJECTS", 1)
    entries = {
        key: ProjectEntry(
            key=key,
            title=key.title(),
            path=f"projects/{key}",
            status="active",
        )
        for key in ("one", "two")
    }
    with pytest.raises(ValidationError, match="project limit"):
        serialize_registry(entries)


def test_require_project_missing_and_unknown(workspace: WorkspaceRoot) -> None:
    with pytest.raises(ProjectRequiredError):
        require_project(workspace, None)
    with pytest.raises(ProjectRequiredError):
        require_project(workspace, "  ")
    add_registry_entry(workspace, validate_project_key("demo"), "Demo")
    with pytest.raises(ProjectNotFoundError) as excinfo:
        require_project(workspace, "nope")
    assert excinfo.value.details is not None
    assert excinfo.value.details["available_projects"] == ["demo"]
    assert require_project(workspace, "demo").key == "demo"
