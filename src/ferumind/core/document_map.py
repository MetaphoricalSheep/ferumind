"""Structured maps of Markdown documents for lookup-first editing.

This module derives line ranges, heading sections, and structural blocks from
a Markdown document so an assistant can choose the smallest safe edit target.
Every range exposes a content hash so that subsequent edits can be guarded
against concurrent changes.

Line numbers are 1-indexed and refer to the *whole* document (including
frontmatter). Lines are derived with ``content.split("\\n")`` so that
``"\\n".join(lines)`` losslessly reconstructs the document; this keeps line
numbers identical across :func:`build_document_map`,
:func:`read_document_range`, and the granular patch helpers.
"""

from __future__ import annotations

import re
from typing import Literal

from ferumind.core.documents import compute_sha256
from ferumind.core.errors import (
    RangeNotFoundError,
    RangeTooLargeError,
    SectionNotFoundError,
)
from ferumind.core.frontmatter import extract_frontmatter_block, infer_title, parse_frontmatter
from ferumind.core.types import JsonObject, StrictModel

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HR_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_LIST_RE = re.compile(r"^\s*([-*+]\s+|\d+[.)]\s+)")
_BLOCKQUOTE_RE = re.compile(r"^\s*>")

MAX_RANGE_LINES_DEFAULT = 300
MAX_LINES_IN_MAP = 5000
MAX_STRUCTURE_LINES = 100_000

BlockKind = Literal[
    "paragraph",
    "heading",
    "list",
    "code_fence",
    "blockquote",
    "table",
    "horizontal_rule",
    "blank",
    "unknown",
]


class LineRange(StrictModel):
    start_line: int
    end_line: int


class DocumentSection(StrictModel):
    section_id: str
    kind: Literal["preamble", "heading"]
    heading_path: list[str]
    heading_text: str | None = None
    level: int | None = None
    start_line: int
    end_line: int
    content_sha256: str
    #: UTF-8 byte size of the section text — a cheap read-cost hint.
    size_bytes: int


class DocumentBlock(StrictModel):
    block_id: str
    kind: BlockKind
    section_id: str | None
    start_line: int
    end_line: int
    content_sha256: str
    preview: str


class DocumentLine(StrictModel):
    line_number: int
    sha256: str
    text_preview: str


class DocumentMap(StrictModel):
    project_key: str
    path: str
    document_sha256: str
    title: str
    status: str
    total_lines: int
    frontmatter_range: LineRange | None
    body_range: LineRange
    sections: list[DocumentSection]
    blocks: list[DocumentBlock]
    lines: list[DocumentLine] | None = None


class DocumentRangeRead(StrictModel):
    project_key: str
    path: str
    document_sha256: str
    range: LineRange
    range_sha256: str
    content: str
    numbered_content: str | None = None


def split_document_lines(content: str) -> list[str]:
    """Split *content* into 1-indexable lines with lossless round-tripping."""
    return content.split("\n")


def hash_line_range(lines: list[str], start_line: int, end_line: int) -> str:
    """Return the SHA-256 of lines ``start_line..end_line`` (1-indexed, inclusive)."""
    return compute_sha256("\n".join(lines[start_line - 1 : end_line]))


def frontmatter_line_range(content: str) -> tuple[LineRange | None, int]:
    """Return the frontmatter line range (or ``None``) and the body's first line."""
    fm_block, _body = extract_frontmatter_block(content)
    if not fm_block:
        return None, 1
    fm_lines = fm_block.split("\n")
    fm_end_line = len(fm_lines) - 1 if fm_block.endswith("\n") else len(fm_lines)
    return LineRange(start_line=1, end_line=fm_end_line), fm_end_line + 1


def _find_headings(
    lines: list[str], body_start: int, total_lines: int
) -> list[tuple[int, int, str]]:
    """Return ``(line_number, level, text)`` for body headings outside code fences."""
    headings: list[tuple[int, int, str]] = []
    in_code = False
    for ln in range(body_start, total_lines + 1):
        line = lines[ln - 1]
        if _FENCE_RE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue
        match = _HEADING_RE.match(line)
        if match:
            headings.append((ln, len(match.group(1)), match.group(2).strip()))
    return headings


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "section"


def _unique_id(base: str, used: set[str], next_suffixes: dict[str, int]) -> str:
    suffix = next_suffixes.get(base, 1)
    candidate = base if suffix == 1 else f"{base}-{suffix}"
    while candidate in used:
        suffix = max(suffix + 1, 2)
        candidate = f"{base}-{suffix}"
    used.add(candidate)
    next_suffixes[base] = max(suffix + 1, 2)
    return candidate


def derive_sections(lines: list[str], body_start: int, total_lines: int) -> list[DocumentSection]:
    """Derive preamble and heading sections from the document body."""
    if body_start > total_lines:
        return []
    headings = _find_headings(lines, body_start, total_lines)
    sections: list[DocumentSection] = []
    used_ids: set[str] = set()
    next_suffixes: dict[str, int] = {}

    first_heading_line = headings[0][0] if headings else total_lines + 1
    if first_heading_line > body_start:
        preamble_end = first_heading_line - 1
        if _range_has_text(lines, body_start, preamble_end):
            content_sha256, size_bytes = _section_digest(lines, body_start, preamble_end)
            sections.append(
                DocumentSection(
                    section_id=_unique_id("preamble", used_ids, next_suffixes),
                    kind="preamble",
                    heading_path=[],
                    heading_text=None,
                    level=None,
                    start_line=body_start,
                    end_line=preamble_end,
                    content_sha256=content_sha256,
                    size_bytes=size_bytes,
                )
            )

    end_lines = [total_lines] * len(headings)
    next_line_at_level: list[int | None] = [None] * 7
    for index in range(len(headings) - 1, -1, -1):
        line_number, level, _text = headings[index]
        candidates = [
            next_line for next_line in next_line_at_level[1 : level + 1] if next_line is not None
        ]
        if candidates:
            end_lines[index] = min(candidates) - 1
        next_line_at_level[level] = line_number

    stack: list[tuple[int, str]] = []
    for index, (line_number, level, text) in enumerate(headings):
        while stack and stack[-1][0] >= level:
            stack.pop()
        heading_path = [t for _level, t in stack] + [text]
        stack.append((level, text))

        end_line = end_lines[index]
        content_sha256, size_bytes = _section_digest(lines, line_number, end_line)

        sections.append(
            DocumentSection(
                section_id=_unique_id(_slugify(text), used_ids, next_suffixes),
                kind="heading",
                heading_path=heading_path,
                heading_text=text,
                level=level,
                start_line=line_number,
                end_line=end_line,
                content_sha256=content_sha256,
                size_bytes=size_bytes,
            )
        )
    return sections


def _section_digest(lines: list[str], start_line: int, end_line: int) -> tuple[str, int]:
    """Return ``(content_sha256, size_bytes)`` for lines ``start_line..end_line``."""
    text = "\n".join(lines[start_line - 1 : end_line])
    return compute_sha256(text), len(text.encode("utf-8"))


def _range_has_text(lines: list[str], start_line: int, end_line: int) -> bool:
    return any(lines[ln - 1].strip() for ln in range(start_line, end_line + 1))


def _line_kind(line: str) -> BlockKind:
    if _BLOCKQUOTE_RE.match(line):
        return "blockquote"
    if _LIST_RE.match(line):
        return "list"
    if "|" in line:
        return "table"
    return "paragraph"


def derive_blocks(
    lines: list[str],
    body_start: int,
    total_lines: int,
    sections: list[DocumentSection],
) -> list[DocumentBlock]:
    """Derive structural blocks (paragraphs, code fences, lists, ...) from the body."""
    blocks: list[DocumentBlock] = []
    ln = body_start
    index = 0
    section_index = 0
    active_sections: list[DocumentSection] = []
    while ln <= total_lines:
        line = lines[ln - 1]
        stripped = line.strip()
        start = ln

        if _FENCE_RE.match(line):
            fence = stripped[:3]
            ln += 1
            while ln <= total_lines and not lines[ln - 1].strip().startswith(fence):
                ln += 1
            if ln <= total_lines:
                ln += 1
            kind: BlockKind = "code_fence"
        elif stripped == "":
            while ln <= total_lines and lines[ln - 1].strip() == "":
                ln += 1
            kind = "blank"
        elif _HEADING_RE.match(line):
            ln += 1
            kind = "heading"
        elif _HR_RE.match(line):
            ln += 1
            kind = "horizontal_rule"
        else:
            kind = _line_kind(line)
            ln += 1
            while ln <= total_lines:
                nxt = lines[ln - 1]
                nstripped = nxt.strip()
                if (
                    nstripped == ""
                    or _FENCE_RE.match(nxt)
                    or _HEADING_RE.match(nxt)
                    or _HR_RE.match(nxt)
                    or _line_kind(nxt) != kind
                ):
                    break
                ln += 1

        end = ln - 1
        index += 1
        active_sections = [section for section in active_sections if section.end_line >= start]
        while section_index < len(sections) and sections[section_index].start_line <= start:
            section = sections[section_index]
            if section.end_line >= start:
                active_sections.append(section)
            section_index += 1
        section_id = active_sections[-1].section_id if active_sections else None
        blocks.append(
            DocumentBlock(
                block_id=f"block-{index}",
                kind=kind,
                section_id=section_id,
                start_line=start,
                end_line=end,
                content_sha256=hash_line_range(lines, start, end),
                preview=_preview(lines, start, end),
            )
        )
    return blocks


def _preview(lines: list[str], start_line: int, end_line: int) -> str:
    for ln in range(start_line, end_line + 1):
        text = lines[ln - 1].strip()
        if text:
            return text[:80]
    return ""


def _derive_lines(lines: list[str]) -> list[DocumentLine]:
    return [
        DocumentLine(
            line_number=index + 1,
            sha256=compute_sha256(text),
            text_preview=text[:120],
        )
        for index, text in enumerate(lines)
    ]


def build_document_map(
    *,
    content: str,
    project_key: str,
    path: str,
    include_blocks: bool = True,
    include_lines: bool = False,
) -> DocumentMap:
    """Build a structured :class:`DocumentMap` for *content*."""
    total_lines = content.count("\n") + 1
    if total_lines > MAX_STRUCTURE_LINES:
        raise RangeTooLargeError(
            f"Document has {total_lines} lines; structural maps are limited to "
            f"{MAX_STRUCTURE_LINES} lines",
            details={
                "total_lines": total_lines,
                "max_lines": MAX_STRUCTURE_LINES,
                "recommended_action": "Use read_document_range for targeted inspection.",
            },
        )
    lines = split_document_lines(content)
    document_sha256 = compute_sha256(content)
    fm_range, body_start = frontmatter_line_range(content)
    body_range = LineRange(start_line=body_start, end_line=max(body_start, total_lines))

    frontmatter = parse_frontmatter(content)
    body = "\n".join(lines[body_start - 1 :]) if body_start <= total_lines else ""
    title = infer_title(frontmatter, body, path)
    status_value = frontmatter.get("status")
    status = status_value if isinstance(status_value, str) and status_value else "active"

    sections = derive_sections(lines, body_start, total_lines)
    blocks = derive_blocks(lines, body_start, total_lines, sections) if include_blocks else []

    line_models: list[DocumentLine] | None = None
    if include_lines:
        if total_lines > MAX_LINES_IN_MAP:
            msg = (
                f"Document has {total_lines} lines (max {MAX_LINES_IN_MAP} for include_lines); "
                "use read_document_range for specific ranges"
            )
            raise RangeTooLargeError(msg)
        line_models = _derive_lines(lines)

    return DocumentMap(
        project_key=project_key,
        path=path,
        document_sha256=document_sha256,
        title=title,
        status=status,
        total_lines=total_lines,
        frontmatter_range=fm_range,
        body_range=body_range,
        sections=sections,
        blocks=blocks,
        lines=line_models,
    )


def read_document_range(
    *,
    content: str,
    project_key: str,
    path: str,
    start_line: int,
    end_line: int,
    include_line_numbers: bool = True,
    max_lines: int = MAX_RANGE_LINES_DEFAULT,
) -> DocumentRangeRead:
    """Read exact lines ``start_line..end_line`` with a guarding range hash."""
    if start_line < 1:
        raise RangeNotFoundError("start_line must be >= 1")
    if end_line < start_line:
        raise RangeNotFoundError("end_line must be >= start_line")

    lines = split_document_lines(content)
    total_lines = len(lines)
    if start_line > total_lines:
        msg = f"start_line {start_line} is beyond end of file ({total_lines} lines)"
        raise RangeNotFoundError(msg, details={"total_lines": total_lines})
    end_line = min(end_line, total_lines)

    requested = end_line - start_line + 1
    if requested > max_lines:
        msg = f"Requested {requested} lines exceeds the maximum of {max_lines}"
        raise RangeTooLargeError(
            msg,
            details={
                "requested_lines": requested,
                "max_lines": max_lines,
                "total_lines": total_lines,
                "recommended_action": (
                    f"Read the document in chunks of at most {max_lines} lines, or use "
                    "get_document_map to target a smaller section."
                ),
            },
        )

    selected = lines[start_line - 1 : end_line]
    range_content = "\n".join(selected)
    numbered = None
    if include_line_numbers:
        numbered = "\n".join(
            f"{start_line + offset:>6}|{text}" for offset, text in enumerate(selected)
        )

    return DocumentRangeRead(
        project_key=project_key,
        path=path,
        document_sha256=compute_sha256(content),
        range=LineRange(start_line=start_line, end_line=end_line),
        range_sha256=compute_sha256(range_content),
        content=range_content,
        numbered_content=numbered,
    )


def section_target(content: str, section_id: str) -> JsonObject:
    """Return a minimal serializable descriptor for a section (used by patches)."""
    _fm_range, body_start = frontmatter_line_range(content)
    lines = split_document_lines(content)
    total_lines = len(lines)
    for section in derive_sections(lines, body_start, total_lines):
        if section.section_id == section_id:
            return {
                "section_id": section.section_id,
                "start_line": section.start_line,
                "end_line": section.end_line,
                "content_sha256": section.content_sha256,
            }
    raise SectionNotFoundError(f"Section {section_id!r} not found")
