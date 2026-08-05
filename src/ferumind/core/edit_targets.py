"""Resolution and validation of granular edit targets.

This module turns assistant-supplied target descriptors (section ids, line
ranges, search matches, insert anchors) into validated, hash-bearing
locations inside a Markdown document. It is the single place that:

* finds literal/regex matches inside a document,
* resolves a section id or line range to concrete lines,
* rejects targets that overlap protected frontmatter, and
* resolves insert anchors to a concrete insertion index.

All failures are raised as :class:`~ferumind.core.errors.FerumindError`
subclasses carrying a stable structured code.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

import regex

from ferumind.core.document_map import (
    DocumentSection,
    LineRange,
    derive_sections,
    frontmatter_line_range,
    hash_line_range,
    split_document_lines,
)
from ferumind.core.documents import compute_sha256
from ferumind.core.errors import (
    AmbiguousMatchError,
    FrontmatterProtectedError,
    InvalidRegexError,
    MatchNotFoundError,
    RangeNotFoundError,
    RangeTooLargeError,
    SectionNotFoundError,
    ValidationError,
)
from ferumind.core.types import StrictModel

_FENCE_RE = re.compile(r"^\s*(```|~~~)")

MAX_RANGE_PATCH_LINES = 200
MAX_FIND_LIMIT = 100
MAX_CONTEXT_LINES = 10
MAX_REGEX_QUERY_CHARS = 512
MAX_LITERAL_QUERY_CHARS = 4096
MAX_MATCHES = 10_000
REGEX_MATCH_TIMEOUT_SECONDS = 0.25

FindMode = Literal["literal", "regex"]


class DocumentMatch(StrictModel):
    match_id: str
    start_line: int
    end_line: int
    line_start_column: int | None = None
    line_end_column: int | None = None
    matched_text: str
    context_before: list[str]
    context_after: list[str]
    range_sha256: str


class FindInDocumentResult(StrictModel):
    project_key: str
    path: str
    document_sha256: str
    query: str
    mode: FindMode
    matches: list[DocumentMatch]
    count: int


class InsertAnchor(StrictModel):
    kind: Literal["line", "section", "match", "end_of_file"]
    position: Literal["before", "after"] = "after"
    line_number: int | None = None
    section_id: str | None = None
    match_text: str | None = None
    match_occurrence: int | None = None
    expected_anchor_sha256: str | None = None


class ExactEdit(StrictModel):
    """One exact-replace edit within a multi-edit batch.

    ``old_string`` must match the document exactly once (including whitespace
    and newlines) unless ``occurrence`` selects one of several matches.
    """

    old_string: str
    new_string: str
    occurrence: int | None = None


@dataclass(frozen=True)
class RawMatch:
    """A single match located by line and column (all 0-indexed internally)."""

    line_index: int
    col_start: int
    col_end: int
    text: str


def code_fence_line_set(lines: list[str]) -> set[int]:
    """Return the 1-indexed line numbers that fall inside fenced code blocks."""
    inside: set[int] = set()
    in_code = False
    fence = "```"
    for index, line in enumerate(lines):
        match = _FENCE_RE.match(line)
        if match and not in_code:
            in_code = True
            fence = match.group(1)
            inside.add(index + 1)
            continue
        if in_code:
            inside.add(index + 1)
            if line.strip().startswith(fence):
                in_code = False
    return inside


def _compile_regex(query: str, *, case_sensitive: bool) -> regex.Pattern[str]:
    if len(query) > MAX_REGEX_QUERY_CHARS:
        raise InvalidRegexError(
            f"Regular expression exceeds the {MAX_REGEX_QUERY_CHARS}-character limit"
        )
    flags = 0 if case_sensitive else regex.IGNORECASE
    try:
        return regex.compile(query, flags)
    except regex.error as exc:
        raise InvalidRegexError(f"Invalid regular expression: {exc}") from exc


def _iter_line_matches(
    line: str,
    query: str,
    mode: FindMode,
    *,
    case_sensitive: bool,
    compiled: regex.Pattern[str] | None,
    regex_deadline: float | None,
) -> Iterator[tuple[int, int, str]]:
    if mode == "regex":
        if compiled is None or regex_deadline is None:
            raise InvalidRegexError("Regular expression matcher was not initialized")
        remaining = regex_deadline - time.monotonic()
        if remaining <= 0:
            raise InvalidRegexError(
                f"Regular expression exceeded the {REGEX_MATCH_TIMEOUT_SECONDS}s safety timeout"
            )
        try:
            for match in compiled.finditer(line, timeout=remaining):
                if match.end() == match.start():
                    continue
                yield match.start(), match.end(), match.group(0)
        except TimeoutError as exc:
            raise InvalidRegexError(
                f"Regular expression exceeded the {REGEX_MATCH_TIMEOUT_SECONDS}s safety timeout"
            ) from exc
        return
    haystack = line if case_sensitive else line.lower()
    needle = query if case_sensitive else query.lower()
    start = 0
    while True:
        found = haystack.find(needle, start)
        if found < 0:
            return
        yield found, found + len(query), line[found : found + len(query)]
        start = found + max(1, len(needle))


def iter_matches(
    lines: list[str],
    query: str,
    mode: FindMode,
    *,
    case_sensitive: bool,
    include_code_blocks: bool,
) -> list[RawMatch]:
    """Locate every match of *query* across *lines* (respecting code-block filtering)."""
    if mode == "literal" and len(query) > MAX_LITERAL_QUERY_CHARS:
        raise ValidationError(
            f"Literal query exceeds the {MAX_LITERAL_QUERY_CHARS}-character limit"
        )
    compiled = _compile_regex(query, case_sensitive=case_sensitive) if mode == "regex" else None
    regex_deadline = time.monotonic() + REGEX_MATCH_TIMEOUT_SECONDS if mode == "regex" else None
    code_lines: set[int] = set() if include_code_blocks else code_fence_line_set(lines)
    matches: list[RawMatch] = []
    for index, line in enumerate(lines):
        if not include_code_blocks and (index + 1) in code_lines:
            continue
        for col_start, col_end, text in _iter_line_matches(
            line,
            query,
            mode,
            case_sensitive=case_sensitive,
            compiled=compiled,
            regex_deadline=regex_deadline,
        ):
            matches.append(RawMatch(index, col_start, col_end, text))
            if len(matches) > MAX_MATCHES:
                raise ValidationError(
                    f"Query produced more than {MAX_MATCHES} matches; narrow the query",
                    details={"max_matches": MAX_MATCHES},
                )
    return matches


def find_in_document(
    *,
    content: str,
    project_key: str,
    path: str,
    query: str,
    mode: FindMode = "literal",
    case_sensitive: bool = False,
    include_context_lines: int = 2,
    include_code_blocks: bool = True,
    limit: int = 20,
) -> FindInDocumentResult:
    """Find literal/regex matches inside a single document."""
    if not query:
        raise ValidationError("query must not be empty")
    if limit < 1 or limit > MAX_FIND_LIMIT:
        raise ValidationError(f"limit must be between 1 and {MAX_FIND_LIMIT}")
    if include_context_lines < 0 or include_context_lines > MAX_CONTEXT_LINES:
        raise ValidationError(f"include_context_lines must be between 0 and {MAX_CONTEXT_LINES}")

    lines = split_document_lines(content)
    raw_matches = iter_matches(
        lines,
        query,
        mode,
        case_sensitive=case_sensitive,
        include_code_blocks=include_code_blocks,
    )

    matches: list[DocumentMatch] = []
    for position, raw in enumerate(raw_matches, start=1):
        if len(matches) >= limit:
            break
        line_number = raw.line_index + 1
        before_start = max(0, raw.line_index - include_context_lines)
        context_before = lines[before_start : raw.line_index]
        context_after = lines[raw.line_index + 1 : raw.line_index + 1 + include_context_lines]
        matches.append(
            DocumentMatch(
                match_id=f"match-{position}",
                start_line=line_number,
                end_line=line_number,
                line_start_column=raw.col_start + 1,
                line_end_column=raw.col_end + 1,
                matched_text=raw.text,
                context_before=list(context_before),
                context_after=list(context_after),
                range_sha256=compute_sha256(lines[raw.line_index]),
            )
        )

    return FindInDocumentResult(
        project_key=project_key,
        path=path,
        document_sha256=compute_sha256(content),
        query=query,
        mode=mode,
        matches=matches,
        count=len(raw_matches),
    )


def resolve_section(content: str, section_id: str) -> DocumentSection:
    """Resolve a section id to its current :class:`DocumentSection`."""
    lines = split_document_lines(content)
    _fm_range, body_start = frontmatter_line_range(content)
    for section in derive_sections(lines, body_start, len(lines)):
        if section.section_id == section_id:
            return section
    raise SectionNotFoundError(f"Section {section_id!r} not found")


def assert_range_not_in_frontmatter(content: str, start_line: int, end_line: int) -> None:
    """Reject a range that overlaps the protected frontmatter block."""
    fm_range, _body_start = frontmatter_line_range(content)
    if fm_range is not None and start_line <= fm_range.end_line:
        msg = (
            f"Lines {start_line}-{end_line} overlap frontmatter "
            f"(lines {fm_range.start_line}-{fm_range.end_line}); frontmatter is protected"
        )
        raise FrontmatterProtectedError(msg)


def resolve_range(
    content: str,
    start_line: int,
    end_line: int,
    *,
    max_lines: int = MAX_RANGE_PATCH_LINES,
) -> tuple[LineRange, str]:
    """Validate a line range and return it with its content hash."""
    if start_line < 1:
        raise RangeNotFoundError("start_line must be >= 1")
    if end_line < start_line:
        raise RangeNotFoundError("end_line must be >= start_line")
    lines = split_document_lines(content)
    total_lines = len(lines)
    if end_line > total_lines:
        msg = f"Lines {start_line}-{end_line} exceed end of file ({total_lines} lines)"
        raise RangeNotFoundError(msg)
    if end_line - start_line + 1 > max_lines:
        msg = f"Range of {end_line - start_line + 1} lines exceeds the maximum of {max_lines}"
        raise RangeTooLargeError(
            msg,
            details={
                "requested_lines": end_line - start_line + 1,
                "max_lines": max_lines,
                "total_lines": total_lines,
                "recommended_action": (
                    f"Patch at most {max_lines} lines per call; split the edit into smaller "
                    "ranges or use propose_section_patch for a whole section."
                ),
            },
        )
    assert_range_not_in_frontmatter(content, start_line, end_line)
    return LineRange(start_line=start_line, end_line=end_line), hash_line_range(
        lines, start_line, end_line
    )


def partition_matches_by_frontmatter(
    content: str, matches: list[RawMatch]
) -> tuple[list[RawMatch], list[RawMatch]]:
    """Split *matches* into ``(body_matches, frontmatter_matches)``."""
    fm_range, _body_start = frontmatter_line_range(content)
    if fm_range is None:
        return matches, []
    body: list[RawMatch] = []
    frontmatter: list[RawMatch] = []
    for match in matches:
        if (match.line_index + 1) <= fm_range.end_line:
            frontmatter.append(match)
        else:
            body.append(match)
    return body, frontmatter


@dataclass(frozen=True)
class ExactMatch:
    """One exact (possibly multi-line) occurrence located by character offsets."""

    start_offset: int
    end_offset: int
    start_line: int
    end_line: int


def _line_number_at_offset(line_starts: list[int], offset: int) -> int:
    """Return the 1-indexed line containing character *offset* via binary search."""
    lo, hi = 0, len(line_starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def find_exact_matches(content: str, needle: str) -> list[ExactMatch]:
    """Locate every exact, case-sensitive occurrence of *needle* in *content*.

    Unlike :func:`iter_matches`, the needle may span multiple lines. Matches
    are non-overlapping and returned in document order with 1-indexed
    inclusive line spans.
    """
    if not needle:
        raise ValidationError("old_string must not be empty")
    line_starts = [0]
    for index, char in enumerate(content):
        if char == "\n":
            line_starts.append(index + 1)
    matches: list[ExactMatch] = []
    start = 0
    while True:
        found = content.find(needle, start)
        if found < 0:
            return matches
        end = found + len(needle)
        matches.append(
            ExactMatch(
                start_offset=found,
                end_offset=end,
                start_line=_line_number_at_offset(line_starts, found),
                end_line=_line_number_at_offset(line_starts, max(found, end - 1)),
            )
        )
        if len(matches) > MAX_MATCHES:
            raise ValidationError(
                f"old_string produced more than {MAX_MATCHES} matches; expand it "
                "with surrounding context",
                details={"max_matches": MAX_MATCHES},
            )
        start = end


_WHITESPACE_RUN_RE = re.compile(r"[ \t]+")


def has_whitespace_normalized_match(content: str, needle: str) -> bool:
    """Return whether *needle* would match *content* if horizontal whitespace runs
    and trailing line whitespace were collapsed — used to hint at near-misses."""

    def _normalize(text: str) -> str:
        lines = [_WHITESPACE_RUN_RE.sub(" ", line).strip() for line in text.split("\n")]
        return "\n".join(lines)

    normalized_needle = _normalize(needle)
    if not normalized_needle.strip():
        return False
    return normalized_needle in _normalize(content)


def resolve_match_anchor_line(
    content: str,
    match_text: str,
    occurrence: int | None,
    *,
    include_code_blocks: bool = True,
) -> int:
    """Resolve a literal match anchor to a 1-indexed line number."""
    if not match_text:
        raise ValidationError("match_text must not be empty for a match anchor")
    lines = split_document_lines(content)
    matches = iter_matches(
        lines,
        match_text,
        "literal",
        case_sensitive=True,
        include_code_blocks=include_code_blocks,
    )
    body_matches, fm_matches = partition_matches_by_frontmatter(content, matches)
    if fm_matches and not body_matches:
        raise FrontmatterProtectedError(
            f"Match {match_text!r} only occurs in protected frontmatter"
        )
    if not body_matches:
        raise MatchNotFoundError(f"Match {match_text!r} not found")
    if len(body_matches) > 1 and occurrence is None:
        raise AmbiguousMatchError(
            f"Match {match_text!r} occurs {len(body_matches)} times; "
            "specify match_occurrence to disambiguate"
        )
    selected_index = (occurrence or 1) - 1
    if selected_index < 0 or selected_index >= len(body_matches):
        raise MatchNotFoundError(
            f"Occurrence {occurrence} out of range (found {len(body_matches)})"
        )
    return body_matches[selected_index].line_index + 1
