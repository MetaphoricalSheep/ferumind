"""Ferumind skills: installation, index delivery, on-demand bodies, and safety.

A Ferumind skill is workspace-level behaviour text under ``system/skills/``.
These tests guard the boundary that makes it *not* a project document, and the
index-plus-on-demand delivery that keeps procedure bodies out of every
``get_context`` call.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ferumind.core.context import build_context
from ferumind.core.contract_install import CONTRACT_INSTALLS, contract_source_dir
from ferumind.core.errors import SkillNotFoundError, ValidationError
from ferumind.core.frontmatter import parse_frontmatter
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.registry import require_project
from ferumind.core.skills import SKILLS_DIR, list_skills, read_skill

SHIPPED_SKILL = "distilling-durable-knowledge"


def _skill_source_files() -> list[Path]:
    source = contract_source_dir()
    if source is None:  # pragma: no cover - only when not run from a checkout
        pytest.skip("no contract source in this checkout")
    return sorted((source / "skills").glob("*.md"))


def _write_skill(
    workspace: WorkspaceRoot,
    name: str,
    *,
    description: str = "Use when the test needs a skill with a trigger.",
    body: str = "# Procedure\n\nDo the thing.\n",
    declared_name: str | None = None,
) -> Path:
    target = Path(workspace) / SKILLS_DIR / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"---\nname: {declared_name or name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )
    return target


# ── The metadata guard (deliberately separate from the repo-skill guard) ─────


def test_every_ferumind_skill_has_a_matching_name_and_one_line_trigger() -> None:
    """The analogue of the repo-skill guard, kept separate on purpose.

    Repo skills and Ferumind skills share no code, location, or audience.
    A single guard over both would let a change to one silently constrain the
    other.
    """
    skill_files = _skill_source_files()
    assert skill_files, "the skills layer ships at least one skill"
    for skill_file in skill_files:
        metadata = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        assert metadata["name"] == skill_file.stem
        description = metadata["description"]
        assert isinstance(description, str)
        assert description.startswith("Use ")
        assert "\n" not in description


def test_every_skill_source_is_installed_by_the_contract_map() -> None:
    """A skill nobody installs is bytes in the repository that never ship."""
    for skill_file in _skill_source_files():
        source_rel = f"skills/{skill_file.name}"
        assert CONTRACT_INSTALLS[source_rel] == f"{SKILLS_DIR}/{skill_file.name}"


# ── Installation ────────────────────────────────────────────────────────────


def test_bootstrap_installs_the_shipped_skill(workspace: WorkspaceRoot) -> None:
    installed = Path(workspace) / SKILLS_DIR / f"{SHIPPED_SKILL}.md"

    assert installed.is_file()
    assert [skill.name for skill in list_skills(workspace)] == [SHIPPED_SKILL]


def test_force_bootstrap_refreshes_a_stale_installed_skill(workspace: WorkspaceRoot) -> None:
    from bootstrap_workspace import bootstrap

    installed = Path(workspace) / SKILLS_DIR / f"{SHIPPED_SKILL}.md"
    source = contract_source_dir()
    assert source is not None
    expected = (source / "skills" / f"{SHIPPED_SKILL}.md").read_text(encoding="utf-8")
    installed.write_text(
        "---\nname: stale\ndescription: Use never.\n---\nstale\n", encoding="utf-8"
    )

    bootstrap(Path(workspace), force=True)

    assert installed.read_text(encoding="utf-8") == expected


# ── Index delivery: triggers travel, bodies do not ──────────────────────────


def test_get_context_carries_the_trigger_index_and_never_a_body(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    context = build_context(conn, workspace, require_project(workspace, project))

    assert [skill.name for skill in context.skills] == [SHIPPED_SKILL]
    entry = context.skills[0]
    assert entry.description.startswith("Use ")
    assert entry.path == f"{SKILLS_DIR}/{SHIPPED_SKILL}.md"

    body = read_skill(workspace, SHIPPED_SKILL).content_markdown
    serialized = context.model_dump_json()
    assert "Step 1 — Search existing knowledge first" in body
    assert "Step 1 — Search existing knowledge first" not in serialized


def test_skills_bytes_counts_only_the_index(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    context = build_context(conn, workspace, require_project(workspace, project))

    body_bytes = len(read_skill(workspace, SHIPPED_SKILL).content_markdown.encode("utf-8"))
    assert 0 < context.payload.skills_bytes < body_bytes


def test_a_workspace_with_no_skills_yields_an_empty_index(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    for installed in (Path(workspace) / SKILLS_DIR).glob("*.md"):
        installed.unlink()

    context = build_context(conn, workspace, require_project(workspace, project))

    assert context.skills == []
    assert context.payload.skills_bytes == 0


# ── A skill is not a project document ───────────────────────────────────────


def test_a_skill_is_never_a_project_document(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
    project: str,
) -> None:
    """``system/skills/`` sits outside every project, so nothing indexes it."""
    context = build_context(conn, workspace, require_project(workspace, project))

    assert all(SKILLS_DIR not in document.path for document in context.documents)
    assert all(document.folder != "skills" for document in context.documents)
    rows = conn.execute("SELECT path FROM documents WHERE path LIKE '%skills%'").fetchall()
    assert rows == []


def test_role_folders_gained_no_skills_member() -> None:
    """Option B (a project-level role folder) is explicitly not implemented."""
    from ferumind.core.folders import CREATABLE_FOLDERS, ROLE_FOLDERS

    assert "skills" not in ROLE_FOLDERS
    assert "skills" not in CREATABLE_FOLDERS


# ── Reading a body ──────────────────────────────────────────────────────────


def test_read_skill_returns_the_procedure_body(workspace: WorkspaceRoot) -> None:
    skill = read_skill(workspace, SHIPPED_SKILL)

    assert skill.name == SHIPPED_SKILL
    assert skill.path == f"{SKILLS_DIR}/{SHIPPED_SKILL}.md"
    assert skill.content_markdown.startswith("# Distilling durable knowledge")
    assert "name:" not in skill.content_markdown


def test_unknown_skill_is_a_typed_not_found(workspace: WorkspaceRoot) -> None:
    with pytest.raises(SkillNotFoundError):
        read_skill(workspace, "no-such-skill")


@pytest.mark.parametrize(
    "name",
    ["../rules/00-contract", "a/b", "Upper", "trailing-", "with_underscore", "", "x" * 65],
)
def test_a_malformed_skill_name_is_refused_before_any_path_join(
    workspace: WorkspaceRoot,
    name: str,
) -> None:
    with pytest.raises(ValidationError):
        read_skill(workspace, name)


def test_a_symlinked_skill_is_never_followed(
    workspace: WorkspaceRoot,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: escaped\ndescription: Use never.\n---\nsecret\n", "utf-8")
    (Path(workspace) / SKILLS_DIR / "escaped.md").symlink_to(outside)

    with pytest.raises(SkillNotFoundError):
        read_skill(workspace, "escaped")
    assert "escaped" not in [skill.name for skill in list_skills(workspace)]


def test_a_malformed_skill_is_skipped_rather_than_breaking_the_index(
    workspace: WorkspaceRoot,
) -> None:
    """One bad file must not fail get_context for a project that never uses skills."""
    (Path(workspace) / SKILLS_DIR / "broken.md").write_text("---\nnot: yaml: ---\n", "utf-8")
    _write_skill(workspace, "mismatched", declared_name="something-else")

    names = [skill.name for skill in list_skills(workspace)]

    assert names == [SHIPPED_SKILL]
