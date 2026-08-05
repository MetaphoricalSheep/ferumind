"""Project registry backed by ``workspace/system/projects.yml``.

The YAML registry is the source of truth for which projects exist; the
database carries no copy (product/spec-versioning.md §2.1). ``project`` on a
tool call is an assertion validated here — never an override.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, cast

import yaml
from pydantic import Field

from ferumind.core.errors import (
    ProjectNotFoundError,
    ProjectRequiredError,
    ValidationError,
)
from ferumind.core.file_io import atomic_write_text
from ferumind.core.paths import ProjectKey, WorkspaceRoot, contained_path
from ferumind.core.types import JsonValue, StrictModel
from ferumind.core.yaml_safe import safe_load_yaml

_VALID_PROJECT_KEY = re.compile(r"[a-z][a-z0-9-]*")
MAX_PROJECT_KEY_CHARS = 64
MAX_PROJECT_TITLE_CHARS = 512
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_REGISTRY_PROJECTS = 4096


class ProjectEntry(StrictModel):
    """A registry-backed project entry exposed at tool boundaries."""

    key: str = Field(min_length=1, max_length=MAX_PROJECT_KEY_CHARS)
    title: str = Field(min_length=1, max_length=MAX_PROJECT_TITLE_CHARS)
    path: str = Field(min_length=1, max_length=128)
    status: Literal["active"]


def validate_project_key(key: str) -> ProjectKey:
    """Validate and return a *ProjectKey*, raising ``ValidationError`` otherwise."""
    if not key:
        raise ValidationError("Project key must not be empty")
    if len(key) > MAX_PROJECT_KEY_CHARS:
        raise ValidationError(f"Project key exceeds the {MAX_PROJECT_KEY_CHARS}-character limit")
    if _VALID_PROJECT_KEY.fullmatch(key) is None:
        msg = (
            f"Invalid project key {key!r}: must start with a letter, "
            "contain only lowercase letters, digits, and hyphens"
        )
        raise ValidationError(msg)
    return ProjectKey(key)


def registry_path(workspace: WorkspaceRoot) -> Path:
    return contained_path(workspace, "system/projects.yml")


def project_dir(workspace: WorkspaceRoot, key: str) -> Path:
    validated = validate_project_key(key)
    return contained_path(workspace, f"projects/{validated}")


def load_registry(workspace: WorkspaceRoot) -> dict[str, ProjectEntry]:
    """Load the registry, failing closed if its source-of-truth shape is invalid."""
    path = registry_path(workspace)
    if not path.is_file():
        return {}
    try:
        if path.stat().st_size > MAX_REGISTRY_BYTES:
            raise ValidationError(
                f"Project registry exceeds the {MAX_REGISTRY_BYTES}-byte limit",
                details={"max_bytes": MAX_REGISTRY_BYTES},
            )
        data = safe_load_yaml(
            path.read_text(encoding="utf-8"),
            max_bytes=MAX_REGISTRY_BYTES,
            max_tokens=50_000,
        )
    except (yaml.YAMLError, OSError) as exc:
        raise ValidationError("Project registry is unreadable or invalid YAML") from exc
    if not isinstance(data, dict):
        raise ValidationError("Project registry must be a YAML mapping")
    projects_data = cast(dict[object, object], data).get("projects")
    if not isinstance(projects_data, dict):
        raise ValidationError("Project registry must contain a 'projects' mapping")
    projects_mapping = cast(dict[object, object], projects_data)
    if len(projects_mapping) > MAX_REGISTRY_PROJECTS:
        raise ValidationError(
            f"Project registry exceeds the {MAX_REGISTRY_PROJECTS}-project limit",
            details={"max_projects": MAX_REGISTRY_PROJECTS},
        )
    result: dict[str, ProjectEntry] = {}
    for key, entry in projects_mapping.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise ValidationError(
                "Every project registry entry must be a mapping keyed by a string"
            )
        validate_project_key(key)
        entry_dict = cast(dict[object, object], entry)
        title = entry_dict.get("title")
        path_value = entry_dict.get("path")
        status = entry_dict.get("status")
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > MAX_PROJECT_TITLE_CHARS
            or any(ord(character) < 32 or ord(character) == 127 for character in title)
        ):
            raise ValidationError(f"Project registry entry {key!r} has an invalid title")
        if path_value != f"projects/{key}":
            raise ValidationError(f"Project registry entry {key!r} has a non-canonical path")
        if status != "active":
            raise ValidationError(f"Project registry entry {key!r} has an invalid status")
        result[key] = ProjectEntry(
            key=key,
            title=title,
            path=f"projects/{key}",
            status="active",
        )
    return result


def save_registry(workspace: WorkspaceRoot, registry: dict[str, ProjectEntry]) -> None:
    path = registry_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, serialize_registry(registry))


def serialize_registry(registry: dict[str, ProjectEntry]) -> str:
    """Return the canonical YAML representation of a registry."""
    if len(registry) > MAX_REGISTRY_PROJECTS:
        raise ValidationError(
            f"Project registry exceeds the {MAX_REGISTRY_PROJECTS}-project limit",
            details={"max_projects": MAX_REGISTRY_PROJECTS},
        )
    projects: dict[str, dict[str, str]] = {}
    for key, entry in sorted(registry.items()):
        projects[key] = {"title": entry.title, "path": entry.path, "status": entry.status}
    serialized = yaml.safe_dump(
        {"projects": projects}, default_flow_style=False, allow_unicode=True
    )
    size_bytes = len(serialized.encode("utf-8"))
    if size_bytes > MAX_REGISTRY_BYTES:
        raise ValidationError(
            f"Serialized project registry exceeds the {MAX_REGISTRY_BYTES}-byte limit",
            details={"size_bytes": size_bytes, "max_bytes": MAX_REGISTRY_BYTES},
        )
    return serialized


def add_registry_entry(workspace: WorkspaceRoot, key: ProjectKey, title: str) -> ProjectEntry:
    registry = load_registry(workspace)
    entry = ProjectEntry(key=str(key), title=title, path=f"projects/{key}", status="active")
    registry[str(key)] = entry
    save_registry(workspace, registry)
    return entry


def remove_registry_entry(workspace: WorkspaceRoot, key: str) -> bool:
    """Remove a project from the registry. Returns whether it existed."""
    registry = load_registry(workspace)
    if key not in registry:
        return False
    del registry[key]
    save_registry(workspace, registry)
    return True


def list_entries(workspace: WorkspaceRoot) -> list[ProjectEntry]:
    return [entry for _key, entry in sorted(load_registry(workspace).items())]


def require_project(workspace: WorkspaceRoot, project: str | None) -> ProjectEntry:
    """Validate the ``project`` assertion against the registry.

    Missing/empty → ``PROJECT_REQUIRED``; unknown → ``PROJECT_NOT_FOUND``
    (spec-mcp §1).
    """
    if project is None or not project.strip():
        raise ProjectRequiredError(
            "The 'project' argument is required on every project-scoped call"
        )
    registry = load_registry(workspace)
    entry = registry.get(project)
    if entry is None:
        available = sorted(registry)
        available_values: list[JsonValue] = list(available)
        msg = f"Project {project!r} not found. Available projects: {available}"
        raise ProjectNotFoundError(msg, details={"available_projects": available_values})
    return entry
