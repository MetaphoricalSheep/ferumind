"""Ferumind skills: on-demand procedure text the server hands a chat agent.

A Ferumind skill is workspace-level behaviour, installed from
``product/contract/skills/`` into ``system/skills/`` beside ``system/rules/``.
It is **not** a project document: ``folder_of`` is never asked about it, it is
never indexed, and it never appears in ``get_context.documents``. It is also
not a repo skill — ``.opencode/skills/`` is read by agents building this
repository and shares no code, location, or delivery path with this module.

Delivery is index-plus-on-demand (product D7). ``get_context`` carries only
``name`` + one-line trigger per skill; the body is fetched by name when the
trigger matches. Bodies are deliberately absent from the context payload: the
rules payload is a contended, uncapped budget and a procedure most chats never
need must not be paid for on every call of every chat on every project.

There is no cadence, ``last_run``, or due-now reporting. Every trigger in
practice is situational rather than temporal, and per-agent mutable state has
no home in a server that is stateless per call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from ferumind.core.errors import FrontmatterInvalidError, SkillNotFoundError, ValidationError
from ferumind.core.frontmatter import extract_frontmatter_block, parse_frontmatter
from ferumind.core.paths import PathSafetyError, WorkspaceRoot, contained_path

#: Workspace-relative directory holding installed skills.
SKILLS_DIR: Final = "system/skills"

#: A skill name is its filename stem: lowercase, hyphen-separated, no paths.
#: Enforcing the shape here is what makes ``read_skill(name)`` safe without a
#: second containment argument — a name can never become a traversal.
SKILL_NAME_RE: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_MAX_NAME_LENGTH: Final = 64


@dataclass(frozen=True, slots=True)
class SkillSummary:
    """One index entry: enough to decide whether to fetch the body."""

    name: str
    description: str
    path: str


@dataclass(frozen=True, slots=True)
class SkillDocument:
    """A skill's full procedure text, fetched on demand."""

    name: str
    description: str
    path: str
    content_markdown: str


def is_valid_skill_name(name: str) -> bool:
    """Return whether *name* is a well-formed skill name."""
    return (
        bool(name) and len(name) <= _MAX_NAME_LENGTH and SKILL_NAME_RE.fullmatch(name) is not None
    )


def list_skills(workspace: WorkspaceRoot) -> list[SkillSummary]:
    """Return every installed skill, by name, deterministically ordered.

    A malformed skill file is skipped rather than raising: one bad file must
    not make ``get_context`` fail for a project that never uses skills. The
    metadata guard in the test suite is what keeps malformed skills from
    shipping in the first place.
    """
    try:
        skills_dir = contained_path(workspace, SKILLS_DIR)
    except PathSafetyError:
        return []
    if not skills_dir.is_dir():
        return []

    summaries: list[SkillSummary] = []
    for entry in sorted(skills_dir.glob("*.md")):
        if not entry.is_file() or entry.is_symlink():
            continue
        parsed = _parse_skill(entry.stem, entry.read_text(encoding="utf-8"), tolerant=True)
        if parsed is not None:
            summaries.append(SkillSummary(parsed.name, parsed.description, parsed.path))
    return summaries


def read_skill(workspace: WorkspaceRoot, name: str) -> SkillDocument:
    """Return one installed skill's body by name.

    Raises :class:`ValidationError` for a malformed name and
    :class:`SkillNotFoundError` when no such skill is installed.
    """
    if not is_valid_skill_name(name):
        msg = f"Skill name {name!r} is not a lowercase, hyphen-separated identifier."
        raise ValidationError(msg, details={"name": name})
    try:
        path = contained_path(workspace, f"{SKILLS_DIR}/{name}.md")
    except PathSafetyError as exc:
        raise SkillNotFoundError(f"No skill named {name!r}.", details={"name": name}) from exc
    if not path.is_file() or path.is_symlink():
        raise SkillNotFoundError(f"No skill named {name!r}.", details={"name": name})

    parsed = _parse_skill(name, path.read_text(encoding="utf-8"), tolerant=False)
    if parsed is None:  # pragma: no cover - tolerant=False always parses or raises
        raise SkillNotFoundError(f"No skill named {name!r}.", details={"name": name})
    return parsed


def _parse_skill(stem: str, raw: str, *, tolerant: bool) -> SkillDocument | None:
    """Parse a skill file into name, trigger description, and body."""
    try:
        mapping = parse_frontmatter(raw)
        _block, body = extract_frontmatter_block(raw)
    except FrontmatterInvalidError:
        if tolerant:
            return None
        raise

    name = mapping.get("name")
    description = mapping.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        return None if tolerant else _invalid(stem, "name and description must be strings")
    name = name.strip()
    description = " ".join(description.split())
    if name != stem or not is_valid_skill_name(name) or not description:
        return None if tolerant else _invalid(stem, "name must match the filename and be valid")

    return SkillDocument(
        name=name,
        description=description,
        path=f"{SKILLS_DIR}/{stem}.md",
        content_markdown=body.strip() + "\n",
    )


def _invalid(stem: str, reason: str) -> SkillDocument:
    msg = f"Skill {stem!r} is malformed: {reason}."
    raise ValidationError(msg, details={"name": stem})


__all__ = [
    "SKILLS_DIR",
    "SKILL_NAME_RE",
    "SkillDocument",
    "SkillSummary",
    "is_valid_skill_name",
    "list_skills",
    "read_skill",
]
