"""Tests for document maps, ranges, and lookup targets."""

from __future__ import annotations

import pytest

from lattice.core import document_map as document_map_core
from lattice.core.document_map import (
    build_document_map,
    frontmatter_line_range,
    read_document_range,
    split_document_lines,
)
from lattice.core.documents import compute_sha256
from lattice.core.edit_targets import find_in_document
from lattice.core.errors import (
    InvalidRegexError,
    RangeNotFoundError,
    RangeTooLargeError,
    ValidationError,
)
from lattice.core.frontmatter import generate_frontmatter

FM = generate_frontmatter(doc_id="doc_x", project_key="demo", title="Doc")
BODY = (
    "intro paragraph\n"
    "\n"
    "# Title\n"
    "\n"
    "## Section One\n"
    "\n"
    "text one\n"
    "\n"
    "```python\n"
    "# a comment heading in code\n"
    "```\n"
    "\n"
    "## Section Two\n"
    "\n"
    "text two\n"
)
CONTENT = FM + BODY


def test_map_sections_skip_code_fences_and_hash_ranges() -> None:
    doc_map = build_document_map(content=CONTENT, project_key="demo", path="canvases/d.md")
    titles = [s.heading_text for s in doc_map.sections if s.kind == "heading"]
    assert titles == ["Title", "Section One", "Section Two"]
    assert doc_map.sections[0].kind == "preamble"
    lines = split_document_lines(CONTENT)
    for section in doc_map.sections:
        expected = compute_sha256("\n".join(lines[section.start_line - 1 : section.end_line]))
        assert section.content_sha256 == expected
    assert doc_map.document_sha256 == compute_sha256(CONTENT)


def test_map_heading_paths_nest() -> None:
    doc_map = build_document_map(content=CONTENT, project_key="demo", path="canvases/d.md")
    section_one = next(s for s in doc_map.sections if s.heading_text == "Section One")
    assert section_one.heading_path == ["Title", "Section One"]


def test_map_blocks_cover_body() -> None:
    doc_map = build_document_map(content=CONTENT, project_key="demo", path="canvases/d.md")
    kinds = {b.kind for b in doc_map.blocks}
    assert "code_fence" in kinds
    assert "heading" in kinds
    assert "paragraph" in kinds


def test_large_repeated_heading_map_is_deterministic() -> None:
    heading_count = 3_000
    content = "\n".join(f"# Repeated\n\nparagraph {index}" for index in range(heading_count))

    first = build_document_map(content=content, project_key="demo", path="canvases/large.md")
    second = build_document_map(content=content, project_key="demo", path="canvases/large.md")

    assert len(first.sections) == heading_count
    assert first.sections[-1].section_id == f"repeated-{heading_count}"
    assert first.blocks == second.blocks
    assert first.sections == second.sections


def test_map_rejects_pathological_line_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(document_map_core, "MAX_STRUCTURE_LINES", 3)
    with pytest.raises(RangeTooLargeError, match="limited to 3 lines"):
        build_document_map(content="one\ntwo\nthree\nfour", project_key="demo", path="d.md")


def test_frontmatter_line_range() -> None:
    fm_range, body_start = frontmatter_line_range(CONTENT)
    assert fm_range is not None
    assert fm_range.start_line == 1
    assert body_start == fm_range.end_line + 1
    assert frontmatter_line_range("no fm") == (None, 1)


def test_read_document_range_hashes_and_numbers() -> None:
    result = read_document_range(
        content=CONTENT, project_key="demo", path="d.md", start_line=1, end_line=3
    )
    lines = split_document_lines(CONTENT)
    assert result.content == "\n".join(lines[0:3])
    assert result.range_sha256 == compute_sha256(result.content)
    assert result.numbered_content is not None
    assert result.numbered_content.splitlines()[0].strip().startswith("1|")


def test_read_document_range_bounds() -> None:
    with pytest.raises(RangeNotFoundError):
        read_document_range(
            content=CONTENT, project_key="demo", path="d.md", start_line=9999, end_line=10000
        )
    with pytest.raises(RangeTooLargeError):
        read_document_range(
            content="a\n" * 1000, project_key="demo", path="d.md", start_line=1, end_line=1000
        )


def test_find_in_document_literal_and_context() -> None:
    result = find_in_document(
        content=CONTENT, project_key="demo", path="d.md", query="text", limit=10
    )
    assert result.count == 2
    assert result.matches[0].matched_text == "text"
    assert result.matches[0].context_after or result.matches[0].context_before


def test_find_in_document_regex_and_code_filtering() -> None:
    result = find_in_document(
        content=CONTENT,
        project_key="demo",
        path="d.md",
        query=r"comment \w+",
        mode="regex",
        include_code_blocks=False,
    )
    assert result.count == 0
    with pytest.raises(InvalidRegexError):
        find_in_document(
            content=CONTENT, project_key="demo", path="d.md", query="[unclosed", mode="regex"
        )
    with pytest.raises(ValidationError):
        find_in_document(content=CONTENT, project_key="demo", path="d.md", query="")


def test_find_in_document_times_out_catastrophic_regex() -> None:
    with pytest.raises(InvalidRegexError, match="timeout"):
        find_in_document(
            content=("a" * 100_000) + "!",
            project_key="demo",
            path="d.md",
            query=r"(a+)+$",
            mode="regex",
        )


def test_find_in_document_caps_literal_query_length() -> None:
    with pytest.raises(ValidationError, match="character limit"):
        find_in_document(
            content="short",
            project_key="demo",
            path="d.md",
            query="x" * 4097,
        )
