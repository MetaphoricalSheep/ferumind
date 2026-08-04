"""Markdown document parsing and modeling for the v2 layout."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from lattice.core.errors import FrontmatterInvalidError
from lattice.core.folders import Folder, default_edit_policy, folder_of
from lattice.core.frontmatter import (
    DOCUMENT_TYPE,
    REQUIRED_FRONTMATTER_KEYS,
    infer_title,
    parse_frontmatter,
    validate_edit_policy,
    validate_status,
)
from lattice.core.types import JsonObject


class ParsedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_key: str
    path: str
    folder: Folder
    title: str
    status: str
    edit_policy: str
    #: Whether edit_policy was explicit in frontmatter or a folder default.
    edit_policy_explicit: bool
    content: str
    body: str
    sha256: str
    frontmatter: JsonObject
    created_at: str
    updated_at: str


def compute_sha256(content: str) -> str:
    """Compute the SHA-256 hex digest of a content string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _frontmatter_str(frontmatter: JsonObject, key: str, default: str = "") -> str:
    val = frontmatter.get(key)
    if isinstance(val, str):
        return val
    return default


def _validate_identity(frontmatter: JsonObject, project_key: str) -> None:
    """Fail closed on partial or contradictory managed-document identity."""
    identity_values = [frontmatter.get(key) for key in REQUIRED_FRONTMATTER_KEYS]
    if all(value is None for value in identity_values):
        return
    for key, value in zip(REQUIRED_FRONTMATTER_KEYS, identity_values, strict=True):
        if not isinstance(value, str) or not value.strip():
            raise FrontmatterInvalidError(
                f"Frontmatter identity is partial or invalid: {key!r} is required"
            )
    if frontmatter["type"] != DOCUMENT_TYPE:
        raise FrontmatterInvalidError(
            f"Frontmatter type must be {DOCUMENT_TYPE!r} for a managed document"
        )
    if frontmatter["project"] != project_key:
        raise FrontmatterInvalidError(
            "Frontmatter project does not match the asserted project",
            details={"asserted_project": project_key},
        )


def parse_document_content(content: str, *, project_key: str, path: str) -> ParsedDocument:
    """Parse Markdown *content* at project-relative *path* into a typed document.

    Resolves the role folder from the path and the effective edit policy
    (explicit frontmatter value or folder default). Invalid ``status`` /
    ``edit_policy`` values raise ``FrontmatterInvalidError``; a missing
    ``status`` defaults to ``active``.
    """
    sha256 = compute_sha256(content)
    frontmatter = parse_frontmatter(content)
    _validate_identity(frontmatter, project_key)
    body_match = re.match(r"^---\s*\n.*?\n---\s*\n?", content, re.DOTALL)
    body = content[body_match.end() :] if body_match else content

    folder = folder_of(path)

    raw_status = frontmatter.get("status")
    if "status" in frontmatter and not isinstance(raw_status, str):
        raise FrontmatterInvalidError("Frontmatter status must be a string")
    status = raw_status if isinstance(raw_status, str) else "active"
    validate_status(status)

    explicit_policy = frontmatter.get("edit_policy")
    if "edit_policy" in frontmatter and not isinstance(explicit_policy, str):
        raise FrontmatterInvalidError("Frontmatter edit_policy must be a string")
    if isinstance(explicit_policy, str):
        edit_policy = validate_edit_policy(explicit_policy)
        edit_policy_explicit = True
    else:
        edit_policy = default_edit_policy(folder)
        edit_policy_explicit = False

    stem = Path(path).stem
    return ParsedDocument(
        id=_frontmatter_str(frontmatter, "id", stem),
        project_key=project_key,
        path=path,
        folder=folder,
        title=infer_title(frontmatter, body, path),
        status=status,
        edit_policy=edit_policy,
        edit_policy_explicit=edit_policy_explicit,
        content=content,
        body=body,
        sha256=sha256,
        frontmatter=frontmatter,
        created_at=_frontmatter_str(frontmatter, "created"),
        updated_at=_frontmatter_str(frontmatter, "updated"),
    )


def parse_document(file_path: Path, *, project_key: str, project_root: Path) -> ParsedDocument:
    """Read and parse a Markdown file inside a project."""
    content = file_path.read_text(encoding="utf-8")
    rel_path = file_path.relative_to(project_root).as_posix()
    return parse_document_content(content, project_key=project_key, path=rel_path)
