"""Markdown document parsing and modeling for the format 1 layout."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ferumind.core.errors import FrontmatterInvalidError
from ferumind.core.folders import Folder, default_edit_policy, folder_of
from ferumind.core.frontmatter import (
    DOCUMENT_TYPE,
    REQUIRED_FRONTMATTER_KEYS,
    infer_title,
    parse_frontmatter,
    validate_description,
    validate_edit_policy,
    validate_status,
)
from ferumind.core.types import JsonObject


class ParsedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_key: str
    path: str
    folder: Folder
    title: str
    #: What this document is for, in one or two sentences. Required and
    #: non-empty on every managed document; empty only for Markdown that
    #: carries no identity frontmatter and is therefore not a managed
    #: document at all.
    description: str
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


@dataclass(frozen=True, slots=True)
class DocumentInspection:
    """Canonical structural parse used by full reads and tolerant diagnostics.

    Diagnostics sometimes need to report a bad required description while
    continuing with checks that do not depend on it.  Keeping that tolerance
    here prevents a second frontmatter/status/policy parser from drifting away
    from :func:`parse_document_content`.
    """

    frontmatter: JsonObject
    managed: bool
    folder: Folder
    description: str | None
    description_valid: bool
    status: str
    edit_policy: str
    edit_policy_explicit: bool
    body: str


def compute_sha256(content: str) -> str:
    """Compute the SHA-256 hex digest of a content string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _frontmatter_str(frontmatter: JsonObject, key: str, default: str = "") -> str:
    val = frontmatter.get(key)
    if isinstance(val, str):
        return val
    return default


def _validate_identity(frontmatter: JsonObject, project_key: str) -> bool:
    """Fail closed on partial or contradictory managed-document identity.

    Returns whether the document is *managed*. The caller needs that answer
    anyway — the ``description`` invariant is scoped to managed documents —
    and deriving it here keeps one definition of managed identity rather than
    letting a second one drift alongside it.
    """
    identity_values = [frontmatter.get(key) for key in REQUIRED_FRONTMATTER_KEYS]
    if all(value is None for value in identity_values):
        return False
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
    return True


def _document_description(frontmatter: JsonObject, *, path: str, managed: bool) -> str:
    """Resolve the required ``description``, naming the file when it is wrong.

    Making a required key required is a fail-closed change, and the direction
    is deliberate: a malformed document is refused rather than silently
    indexed with a field missing. That is only useful if the refusal says
    which file to go and fix, so the path is added to the message here — the
    validator itself has no idea what it is validating.

    There is no conditional on format. A managed document has a description in
    every format the build supports; unmanaged Markdown is not a managed
    document and carries none.
    """
    if not managed:
        return ""
    try:
        return validate_description(frontmatter.get("description"))
    except FrontmatterInvalidError as exc:
        raise FrontmatterInvalidError(
            f"{path}: {exc}",
            details={"path": path, "frontmatter_key": "description"},
        ) from exc


def inspect_document_content(
    content: str,
    *,
    project_key: str,
    path: str,
    require_description: bool = True,
) -> DocumentInspection:
    """Canonically inspect a document, optionally tolerating its description.

    ``require_description=False`` is for report-only format diagnostics.  It
    does not make a malformed description valid: ``description_valid`` stays
    false, so callers must not feed that document into normal read/index paths.
    Identity, role, status, and edit-policy validation remain fail closed.
    """
    frontmatter = parse_frontmatter(content)
    managed = _validate_identity(frontmatter, project_key)
    description: str | None = None
    description_valid = not managed
    if managed:
        try:
            description = _document_description(frontmatter, path=path, managed=True)
            description_valid = True
        except FrontmatterInvalidError:
            if require_description:
                raise

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

    return DocumentInspection(
        frontmatter=frontmatter,
        managed=managed,
        folder=folder,
        description=description,
        description_valid=description_valid,
        status=status,
        edit_policy=edit_policy,
        edit_policy_explicit=edit_policy_explicit,
        body=body,
    )


def parse_document_content(content: str, *, project_key: str, path: str) -> ParsedDocument:
    """Parse Markdown *content* at project-relative *path* into a typed document.

    Resolves the role folder from the path and the effective edit policy
    (explicit frontmatter value or folder default). Invalid ``status`` /
    ``edit_policy`` values raise ``FrontmatterInvalidError``; a missing
    ``status`` defaults to ``active``.
    """
    sha256 = compute_sha256(content)
    inspection = inspect_document_content(content, project_key=project_key, path=path)
    frontmatter = inspection.frontmatter
    description = inspection.description or ""
    body = inspection.body
    folder = inspection.folder

    stem = Path(path).stem
    return ParsedDocument(
        id=_frontmatter_str(frontmatter, "id", stem),
        project_key=project_key,
        path=path,
        folder=folder,
        title=infer_title(frontmatter, body, path),
        description=description,
        status=inspection.status,
        edit_policy=inspection.edit_policy,
        edit_policy_explicit=inspection.edit_policy_explicit,
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
