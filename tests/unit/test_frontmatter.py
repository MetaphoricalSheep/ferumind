"""Tests for frontmatter v2 parsing, generation, and protected keys."""

from __future__ import annotations

import pytest

from lattice.core.errors import FrontmatterInvalidError
from lattice.core.frontmatter import (
    PROTECTED_FRONTMATTER_KEYS,
    extract_frontmatter_block,
    generate_frontmatter,
    infer_title,
    is_managed_markdown,
    parse_frontmatter,
    refresh_updated_if_managed,
    set_frontmatter_updated,
    validate_edit_policy,
    validate_status,
)


def test_generate_frontmatter_is_managed_and_parses_back() -> None:
    fm = generate_frontmatter(doc_id="doc_abc", project_key="demo", title="A Plan")
    content = fm + "# A Plan\n"
    parsed = parse_frontmatter(content)
    assert parsed["id"] == "doc_abc"
    assert parsed["type"] == "document"
    assert parsed["project"] == "demo"
    assert parsed["status"] == "active"
    assert "edit_policy" not in parsed
    assert is_managed_markdown(content)


def test_generate_frontmatter_with_explicit_policy() -> None:
    fm = generate_frontmatter(
        doc_id="doc_abc", project_key="demo", title="Rules", edit_policy="ask-human"
    )
    assert parse_frontmatter(fm)["edit_policy"] == "ask-human"


def test_generate_frontmatter_validates_inputs() -> None:
    with pytest.raises(FrontmatterInvalidError):
        generate_frontmatter(doc_id="x", project_key="demo", title="t", status="bogus")
    with pytest.raises(FrontmatterInvalidError):
        generate_frontmatter(doc_id="x", project_key="demo", title="t", edit_policy="bogus")


@pytest.mark.parametrize("status", ["active", "gated", "frozen", "archived"])
def test_validate_status_accepts_v2_statuses(status: str) -> None:
    assert validate_status(status) == status


def test_validate_status_rejects_v1_statuses() -> None:
    for stale in ("draft", "review", "superseded", "stale"):
        with pytest.raises(FrontmatterInvalidError):
            validate_status(stale)


@pytest.mark.parametrize("policy", ["free", "append", "propose-first", "ask-human"])
def test_validate_edit_policy_accepts_v2_policies(policy: str) -> None:
    assert validate_edit_policy(policy) == policy


def test_protected_keys_cover_identity_and_updated() -> None:
    assert PROTECTED_FRONTMATTER_KEYS == ("id", "type", "project", "created", "updated")


def test_parse_frontmatter_rejects_present_but_invalid_blocks() -> None:
    assert parse_frontmatter("no frontmatter") == {}
    with pytest.raises(FrontmatterInvalidError):
        parse_frontmatter("---\n- just\n- a list\n---\nbody")
    with pytest.raises(FrontmatterInvalidError):
        parse_frontmatter("---\n{ not: yaml: at all\n---\n")


@pytest.mark.parametrize(
    "content",
    [
        "---\nid: doc_x\nproject: demo\n",
        "--- \r\nid: doc_x\r\nproject: demo\r\n",
    ],
)
def test_frontmatter_opening_delimiter_without_close_fails_closed(content: str) -> None:
    with pytest.raises(FrontmatterInvalidError, match="not terminated"):
        parse_frontmatter(content)
    with pytest.raises(FrontmatterInvalidError, match="not terminated"):
        extract_frontmatter_block(content)


def test_parse_frontmatter_rejects_duplicate_keys() -> None:
    with pytest.raises(FrontmatterInvalidError):
        parse_frontmatter("---\nproject: demo\nproject: other\n---\nbody\n")


def test_parse_frontmatter_rejects_yaml_aliases() -> None:
    with pytest.raises(FrontmatterInvalidError):
        parse_frontmatter("---\ntags: &tags [one, two]\ncopy: *tags\n---\nbody\n")


def test_extract_frontmatter_block_round_trips() -> None:
    fm = generate_frontmatter(doc_id="doc_x", project_key="p", title="T")
    content = fm + "body line\n"
    block, body = extract_frontmatter_block(content)
    assert block == fm
    assert body == "body line\n"
    assert block + body == content


def test_set_frontmatter_updated_replaces_and_inserts() -> None:
    fm = generate_frontmatter(doc_id="doc_x", project_key="p", title="T")
    stamped = set_frontmatter_updated(fm, "2030-01-01T00:00:00+00:00")
    assert "updated: 2030-01-01T00:00:00+00:00" in stamped
    bare = "---\nid: doc_x\ntype: document\nproject: p\n---\n"
    stamped_bare = set_frontmatter_updated(bare, "2030-01-01T00:00:00+00:00")
    assert "updated: 2030-01-01T00:00:00+00:00" in stamped_bare


def test_refresh_updated_only_touches_managed_documents() -> None:
    unmanaged = "just some text\n"
    assert refresh_updated_if_managed(unmanaged) == unmanaged
    fm = generate_frontmatter(doc_id="doc_x", project_key="p", title="T")
    content = fm + "body\n"
    refreshed = refresh_updated_if_managed(content)
    assert refreshed.endswith("body\n")
    assert "updated:" in refreshed


def test_infer_title_precedence() -> None:
    assert infer_title({"title": "From FM"}, "# From H1\n", "some-file.md") == "From FM"
    assert infer_title({}, "# From H1\n", "some-file.md") == "From H1"
    assert infer_title({}, "no heading\n", "some-file.md") == "Some File"
