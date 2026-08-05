"""Tests for the pure patch builders (granular edit machinery)."""

from __future__ import annotations

import pytest

from ferumind.core.document_map import build_document_map
from ferumind.core.edit_targets import ExactEdit, InsertAnchor
from ferumind.core.errors import (
    AmbiguousMatchError,
    FrontmatterProtectedError,
    FrontmatterRequiredError,
    MatchNotFoundError,
    PatchConflictError,
    ValidationError,
)
from ferumind.core.frontmatter import generate_frontmatter
from ferumind.core.patches import (
    prepare_exact_replace_patch,
    prepare_frontmatter_patch,
    prepare_insert_patch,
    prepare_multi_edit_patch,
    prepare_range_patch,
    prepare_search_replace_patch,
    prepare_section_patch,
)

FM = generate_frontmatter(doc_id="doc_x", project_key="demo", title="Doc")
BODY = "# Doc\n\n## Alpha\n\nalpha text\n\n## Beta\n\nbeta text\nbeta text\n"
CONTENT = FM + BODY


class TestExactReplace:
    def test_replaces_unique_match(self) -> None:
        prepared = prepare_exact_replace_patch(
            CONTENT, old_string="alpha text", new_string="ALPHA TEXT"
        )
        assert "ALPHA TEXT" in prepared.new_full_content
        assert prepared.proposal_kind == "exact_replace"

    def test_multiline_needles_match(self) -> None:
        prepared = prepare_exact_replace_patch(
            CONTENT, old_string="## Alpha\n\nalpha text", new_string="## Alpha\n\nrewritten"
        )
        assert "rewritten" in prepared.new_full_content

    def test_ambiguous_match_requires_occurrence(self) -> None:
        with pytest.raises(AmbiguousMatchError):
            prepare_exact_replace_patch(CONTENT, old_string="beta text", new_string="x")
        prepared = prepare_exact_replace_patch(
            CONTENT, old_string="beta text", new_string="x", occurrence=2
        )
        assert prepared.new_full_content.count("beta text") == 1

    def test_all_occurrences_need_expected_count(self) -> None:
        with pytest.raises(AmbiguousMatchError):
            prepare_exact_replace_patch(
                CONTENT, old_string="beta text", new_string="x", occurrence="all"
            )
        with pytest.raises(PatchConflictError):
            prepare_exact_replace_patch(
                CONTENT,
                old_string="beta text",
                new_string="x",
                occurrence="all",
                expected_match_count=3,
            )
        prepared = prepare_exact_replace_patch(
            CONTENT,
            old_string="beta text",
            new_string="x",
            occurrence="all",
            expected_match_count=2,
        )
        assert "beta text" not in prepared.new_full_content

    def test_not_found_carries_whitespace_hint(self) -> None:
        with pytest.raises(MatchNotFoundError) as excinfo:
            prepare_exact_replace_patch(CONTENT, old_string="alpha  text", new_string="x")
        assert excinfo.value.details is not None
        assert "whitespace" in str(excinfo.value.details["hint"])

    def test_frontmatter_only_match_is_protected(self) -> None:
        with pytest.raises(FrontmatterProtectedError):
            prepare_exact_replace_patch(CONTENT, old_string="doc_x", new_string="doc_y")

    def test_validation_errors(self) -> None:
        with pytest.raises(ValidationError):
            prepare_exact_replace_patch(CONTENT, old_string="", new_string="x")
        with pytest.raises(ValidationError):
            prepare_exact_replace_patch(CONTENT, old_string="same", new_string="same")


class TestMultiEdit:
    def test_sequential_edits_apply_atomically(self) -> None:
        prepared = prepare_multi_edit_patch(
            CONTENT,
            edits=[
                ExactEdit(old_string="alpha text", new_string="first"),
                ExactEdit(old_string="first", new_string="second"),
            ],
        )
        assert "second" in prepared.new_full_content
        assert "alpha text" not in prepared.new_full_content

    def test_failing_edit_reports_its_index(self) -> None:
        with pytest.raises(MatchNotFoundError) as excinfo:
            prepare_multi_edit_patch(
                CONTENT,
                edits=[
                    ExactEdit(old_string="alpha text", new_string="x"),
                    ExactEdit(old_string="never there", new_string="y"),
                ],
            )
        assert excinfo.value.details is not None
        assert excinfo.value.details["edit_index"] == 1

    def test_empty_batch_rejected(self) -> None:
        with pytest.raises(ValidationError):
            prepare_multi_edit_patch(CONTENT, edits=[])


class TestSectionAndRange:
    def test_section_patch_replaces_section(self) -> None:
        doc_map = build_document_map(content=CONTENT, project_key="demo", path="canvases/d.md")
        alpha = next(s for s in doc_map.sections if s.heading_text == "Alpha")
        prepared = prepare_section_patch(CONTENT, alpha.section_id, "## Alpha\n\nnew alpha\n")
        assert "new alpha" in prepared.new_full_content
        assert "beta text" in prepared.new_full_content
        assert prepared.target_before_sha256 == alpha.content_sha256

    def test_range_patch_refuses_frontmatter(self) -> None:
        with pytest.raises(FrontmatterProtectedError):
            prepare_range_patch(CONTENT, 2, 3, "hacked")

    def test_range_patch_replaces_lines(self) -> None:
        lines = CONTENT.split("\n")
        target_line = lines.index("alpha text") + 1
        prepared = prepare_range_patch(CONTENT, target_line, target_line, "replaced line")
        assert "replaced line" in prepared.new_full_content
        assert "alpha text" not in prepared.new_full_content


class TestSearchReplace:
    def test_single_occurrence(self) -> None:
        prepared = prepare_search_replace_patch(
            CONTENT, find="alpha", replace="omega", case_sensitive=True
        )
        assert "omega text" in prepared.new_full_content

    def test_newline_in_find_is_redirected(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            prepare_search_replace_patch(CONTENT, find="a\nb", replace="x")
        assert excinfo.value.details is not None
        assert excinfo.value.details["recommended_tool"] == "propose_exact_replace_patch"

    def test_all_with_count(self) -> None:
        prepared = prepare_search_replace_patch(
            CONTENT, find="beta text", replace="swapped", occurrence="all", expected_match_count=2
        )
        assert prepared.new_full_content.count("swapped") == 2


class TestInsert:
    def test_end_of_file_append(self) -> None:
        prepared = prepare_insert_patch(CONTENT, InsertAnchor(kind="end_of_file"), "appended line")
        assert prepared.new_full_content.endswith("appended line")

    def test_line_anchor_inside_frontmatter_is_protected(self) -> None:
        with pytest.raises(FrontmatterProtectedError):
            prepare_insert_patch(CONTENT, InsertAnchor(kind="line", line_number=2), "injected")

    def test_match_anchor_inserts_after(self) -> None:
        prepared = prepare_insert_patch(
            CONTENT,
            InsertAnchor(kind="match", match_text="alpha text", position="after"),
            "inserted after alpha",
        )
        body = prepared.new_full_content
        assert body.index("inserted after alpha") > body.index("alpha text")


class TestFrontmatterPatch:
    def test_sets_and_removes_keys(self) -> None:
        prepared = prepare_frontmatter_patch(
            CONTENT, set_values={"status": "frozen"}, remove_keys=[]
        )
        assert "status: frozen" in prepared.new_full_content
        assert prepared.new_full_content.endswith(BODY)

    def test_protected_keys_refused(self) -> None:
        for key in ("id", "type", "project", "created", "updated"):
            with pytest.raises(FrontmatterProtectedError):
                prepare_frontmatter_patch(CONTENT, set_values={key: "x"}, remove_keys=[])

    def test_invalid_status_and_policy_rejected(self) -> None:
        with pytest.raises(ValidationError):
            prepare_frontmatter_patch(CONTENT, set_values={"status": "bogus"}, remove_keys=[])
        with pytest.raises(ValidationError):
            prepare_frontmatter_patch(CONTENT, set_values={"edit_policy": "bogus"}, remove_keys=[])

    def test_removing_missing_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            prepare_frontmatter_patch(CONTENT, set_values={}, remove_keys=["nonexistent"])

    def test_document_without_frontmatter_rejected(self) -> None:
        with pytest.raises(FrontmatterRequiredError):
            prepare_frontmatter_patch("# bare\n", set_values={"status": "active"}, remove_keys=[])


def test_patch_never_strips_managed_frontmatter() -> None:
    # A section patch that would replace the whole body still keeps frontmatter.
    prepared = prepare_exact_replace_patch(
        CONTENT, old_string=BODY.rstrip("\n"), new_string="# minimal"
    )
    assert prepared.new_full_content.startswith("---\n")
    assert "id: doc_x" in prepared.new_full_content
