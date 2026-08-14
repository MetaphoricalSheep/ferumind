"""Tests for document parsing (folder + resolved edit policy)."""

from __future__ import annotations

import pytest

from ferumind.core.documents import (
    compute_sha256,
    inspect_document_content,
    parse_document_content,
)
from ferumind.core.errors import FrontmatterInvalidError, UnknownFolderError
from ferumind.core.frontmatter import (
    MAX_DESCRIPTION_CHARS,
    FrontmatterBehavior,
    generate_frontmatter,
)
from tests.conftest import TEST_DESCRIPTION


def _doc(path: str, *, edit_policy: str | None = None, status: str = "active") -> str:
    fm = generate_frontmatter(
        description=TEST_DESCRIPTION,
        doc_id="doc_x",
        project_key="demo",
        title="T",
        behavior=FrontmatterBehavior(status=status, edit_policy=edit_policy),
    )
    return fm + "# T\n\nbody\n"


def test_parse_resolves_folder_and_default_policy() -> None:
    parsed = parse_document_content(_doc("x"), project_key="demo", path="canvases/x.md")
    assert parsed.folder == "canvases"
    assert parsed.edit_policy == "free"
    assert parsed.edit_policy_explicit is False
    assert parsed.status == "active"
    assert parsed.description == TEST_DESCRIPTION
    assert isinstance(parsed.description, str)
    assert parsed.sha256 == compute_sha256(parsed.content)
    assert parsed.body.startswith("# T")


def test_parse_prefers_explicit_policy() -> None:
    parsed = parse_document_content(
        _doc("x", edit_policy="append"), project_key="demo", path="canvases/log.md"
    )
    assert parsed.edit_policy == "append"
    assert parsed.edit_policy_explicit is True


def test_parse_spine_defaults_to_propose_first() -> None:
    parsed = parse_document_content(_doc("spine"), project_key="demo", path="spine.md")
    assert parsed.folder == "spine"
    assert parsed.edit_policy == "propose-first"


def test_parse_rules_default_to_ask_human() -> None:
    parsed = parse_document_content(_doc("r"), project_key="demo", path="rules/00-project.md")
    assert parsed.edit_policy == "ask-human"


def test_parse_rejects_invalid_status_and_policy() -> None:
    bad_status = "---\nid: doc_x\ntype: document\nproject: demo\nstatus: bogus\n---\n"
    with pytest.raises(FrontmatterInvalidError):
        parse_document_content(bad_status, project_key="demo", path="canvases/x.md")
    bad_policy = "---\nid: doc_x\ntype: document\nproject: demo\nedit_policy: bogus\n---\n"
    with pytest.raises(FrontmatterInvalidError):
        parse_document_content(bad_policy, project_key="demo", path="canvases/x.md")
    non_string_status = "---\nid: doc_x\ntype: document\nproject: demo\nstatus: 1\n---\n"
    with pytest.raises(FrontmatterInvalidError):
        parse_document_content(
            non_string_status,
            project_key="demo",
            path="canvases/x.md",
        )
    non_string_policy = "---\nid: doc_x\ntype: document\nproject: demo\nedit_policy: [free]\n---\n"
    with pytest.raises(FrontmatterInvalidError):
        parse_document_content(
            non_string_policy,
            project_key="demo",
            path="canvases/x.md",
        )


def test_parse_rejects_unknown_folder() -> None:
    with pytest.raises(UnknownFolderError):
        parse_document_content(_doc("x"), project_key="demo", path="stuff/x.md")


def test_parse_unmanaged_content_still_works() -> None:
    parsed = parse_document_content("# Bare\n\ntext\n", project_key="demo", path="memory/bare.md")
    assert parsed.folder == "memory"
    assert parsed.title == "Bare"
    assert parsed.id == "bare"
    assert parsed.status == "active"


@pytest.mark.parametrize(
    "frontmatter",
    [
        "---\nid: doc_x\ntype: document\n---\n",
        "---\nid: doc_x\ntype: other\nproject: demo\n---\n",
        "---\nid: doc_x\ntype: document\nproject: other\n---\n",
    ],
)
def test_parse_rejects_partial_or_contradictory_identity(frontmatter: str) -> None:
    with pytest.raises(FrontmatterInvalidError):
        parse_document_content(frontmatter, project_key="demo", path="memory/x.md")


@pytest.mark.parametrize(
    "description_line",
    [
        "",
        "description: 7\n",
        "description: ''\n",
        "description: '   '\n",
        f"description: {'x' * (MAX_DESCRIPTION_CHARS + 1)}\n",
    ],
    ids=["missing", "non-string", "empty", "whitespace", "overlength"],
)
def test_parse_rejects_invalid_managed_description(description_line: str) -> None:
    content = f"---\nid: doc_x\ntype: document\nproject: demo\n{description_line}---\n# T\n"
    with pytest.raises(FrontmatterInvalidError, match=r"memory/x\.md: Frontmatter description"):
        parse_document_content(content, project_key="demo", path="memory/x.md")


def test_tolerant_inspection_marks_description_invalid_without_weakening_parse() -> None:
    content = "---\nid: doc_x\ntype: document\nproject: demo\n---\n# T\n"

    inspected = inspect_document_content(
        content,
        project_key="demo",
        path="memory/x.md",
        require_description=False,
    )

    assert inspected.managed is True
    assert inspected.description is None
    assert inspected.description_valid is False
    with pytest.raises(FrontmatterInvalidError):
        parse_document_content(content, project_key="demo", path="memory/x.md")
