"""Pure builders that prepare patched document content for granular edits.

These helpers take a document's current content plus a target descriptor and
return a :class:`PreparedPatch` describing the *entire* resulting file. They
do not touch the filesystem or the database; :mod:`ferumind.core.patch_writes` owns
snapshotting, operation recording, and applying. Keeping the transforms here
prevents patch logic from leaking into MCP/CLI layers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

import yaml

from ferumind.core.document_map import (
    frontmatter_line_range,
    hash_line_range,
    split_document_lines,
)
from ferumind.core.documents import compute_sha256
from ferumind.core.edit_targets import (
    ExactEdit,
    ExactMatch,
    FindMode,
    InsertAnchor,
    find_exact_matches,
    has_whitespace_normalized_match,
    iter_matches,
    partition_matches_by_frontmatter,
    resolve_match_anchor_line,
    resolve_range,
    resolve_section,
)
from ferumind.core.errors import (
    AmbiguousMatchError,
    FrontmatterInvalidError,
    FrontmatterProtectedError,
    FrontmatterRequiredError,
    MatchNotFoundError,
    PatchConflictError,
    TargetHashMismatchError,
    ValidationError,
)
from ferumind.core.frontmatter import (
    PROTECTED_FRONTMATTER_KEYS,
    extract_frontmatter_block,
    is_managed_markdown,
    parse_frontmatter,
    refresh_updated_if_managed,
    validate_description,
    validate_edit_policy,
    validate_status,
)
from ferumind.core.types import JsonObject, JsonValue

ProposalKind = Literal[
    "section",
    "range",
    "search_replace",
    "exact_replace",
    "multi_edit",
    "frontmatter",
    "insert",
    "body",
    "full",
]

MAX_MULTI_EDITS = 25


def _empty_target() -> JsonObject:
    return {}


@dataclass
class PreparedPatch:
    """The fully-resolved result of a granular patch, ready for proposal."""

    proposal_kind: ProposalKind
    new_full_content: str
    target_before_sha256: str | None
    target: JsonObject = field(default_factory=_empty_target)


def _replace_lines(lines: list[str], start_line: int, end_line: int, new_content: str) -> str:
    new_lines = new_content.split("\n")
    rebuilt = lines[: start_line - 1] + new_lines + lines[end_line:]
    return "\n".join(rebuilt)


def _finalize(original: str, new_full_content: str) -> str:
    """Refresh ``updated`` for managed docs and guard against frontmatter loss."""
    finalized = refresh_updated_if_managed(new_full_content)
    if is_managed_markdown(original) and not is_managed_markdown(finalized):
        raise FrontmatterRequiredError(
            "Patch would remove required frontmatter (id/type/project) from a managed document"
        )
    return finalized


def prepare_section_patch(content: str, section_id: str, new_content: str) -> PreparedPatch:
    """Replace a single heading-derived section, preserving frontmatter."""
    section = resolve_section(content, section_id)
    lines = split_document_lines(content)
    new_full = _replace_lines(lines, section.start_line, section.end_line, new_content)
    finalized = _finalize(content, new_full)
    target: JsonObject = {
        "kind": "section",
        "section_id": section.section_id,
        "start_line": section.start_line,
        "end_line": section.end_line,
        "section_sha256": section.content_sha256,
        "new_content_sha256": compute_sha256(new_content),
    }
    return PreparedPatch(
        proposal_kind="section",
        new_full_content=finalized,
        target_before_sha256=section.content_sha256,
        target=target,
    )


def prepare_range_patch(
    content: str,
    start_line: int,
    end_line: int,
    new_content: str,
) -> PreparedPatch:
    """Replace a specific line range, refusing to touch frontmatter."""
    line_range, range_sha256 = resolve_range(content, start_line, end_line)
    lines = split_document_lines(content)
    new_full = _replace_lines(lines, line_range.start_line, line_range.end_line, new_content)
    finalized = _finalize(content, new_full)
    target: JsonObject = {
        "kind": "range",
        "start_line": line_range.start_line,
        "end_line": line_range.end_line,
        "range_sha256": range_sha256,
        "new_content_sha256": compute_sha256(new_content),
    }
    return PreparedPatch(
        proposal_kind="range",
        new_full_content=finalized,
        target_before_sha256=range_sha256,
        target=target,
    )


def prepare_search_replace_patch(
    content: str,
    *,
    find: str,
    replace: str,
    mode: FindMode = "literal",
    case_sensitive: bool = False,
    occurrence: int | Literal["all"] = 1,
    expected_match_count: int | None = None,
    include_code_blocks: bool = True,
) -> PreparedPatch:
    """Replace a single selected match or an explicit set of all matches."""
    if not find:
        raise ValidationError("find must not be empty")
    if "\n" in find:
        raise ValidationError(
            "find contains a newline; search/replace matches within single lines only. "
            "Use propose_exact_replace_patch for multi-line exact replacement.",
            details={"recommended_tool": "propose_exact_replace_patch"},
        )

    lines = split_document_lines(content)
    raw_matches = iter_matches(
        lines, find, mode, case_sensitive=case_sensitive, include_code_blocks=include_code_blocks
    )
    body_matches, fm_matches = partition_matches_by_frontmatter(content, raw_matches)
    count = len(body_matches)
    if count == 0:
        # Only block when the query exclusively hits protected frontmatter;
        # body matches must remain editable even if the same text also appears
        # in the frontmatter.
        if fm_matches:
            raise FrontmatterProtectedError(
                f"{find!r} only matches protected frontmatter; frontmatter cannot be patched here"
            )
        raise MatchNotFoundError(
            f"No match found for {find!r}",
            details=_match_not_found_details(content, find),
        )
    if expected_match_count is not None and count != expected_match_count:
        raise PatchConflictError(
            f"Expected {expected_match_count} matches but found {count}; document changed "
            "or the query is broader than expected",
            details={"expected_match_count": expected_match_count, "current_match_count": count},
        )

    if occurrence == "all":
        if expected_match_count is None:
            raise AmbiguousMatchError(
                "occurrence='all' requires expected_match_count to confirm the match total"
            )
        selected = list(body_matches)
    else:
        if occurrence < 1:
            raise ValidationError("occurrence must be >= 1")
        if occurrence > count:
            raise MatchNotFoundError(
                f"Occurrence {occurrence} out of range (found {count} matches)",
                details={"current_match_count": count},
            )
        selected = [body_matches[occurrence - 1]]

    mutable = list(lines)
    # Replace right-to-left so earlier column offsets stay valid within a line.
    for match in sorted(selected, key=lambda m: (m.line_index, m.col_start), reverse=True):
        line = mutable[match.line_index]
        mutable[match.line_index] = line[: match.col_start] + replace + line[match.col_end :]
    new_full = "\n".join(mutable)
    finalized = _finalize(content, new_full)

    occurrence_value: JsonValue = "all" if occurrence == "all" else occurrence
    target: JsonObject = {
        "kind": "search_replace",
        "find": find,
        "replace": replace,
        "mode": mode,
        "case_sensitive": case_sensitive,
        "include_code_blocks": include_code_blocks,
        "occurrence": occurrence_value,
        "match_count": count,
    }
    return PreparedPatch(
        proposal_kind="search_replace",
        new_full_content=finalized,
        target_before_sha256=compute_sha256(find),
        target=target,
    )


def _validate_anchor_hash(content: str, line_number: int, expected: str | None) -> None:
    if expected is None:
        return
    lines = split_document_lines(content)
    actual = hash_line_range(lines, line_number, line_number)
    if actual != expected:
        raise TargetHashMismatchError(
            f"Anchor line {line_number} hash mismatch; the document changed since lookup"
        )


def _resolve_insert_index(content: str, anchor: InsertAnchor) -> int:
    """Resolve an anchor to a 0-indexed insertion position in the line list."""
    lines = split_document_lines(content)
    total_lines = len(lines)
    fm_range, _body_start = frontmatter_line_range(content)

    if anchor.kind == "end_of_file":
        return total_lines

    if anchor.kind == "line":
        if anchor.line_number is None:
            raise ValidationError("line_number is required for a line anchor")
        if anchor.line_number < 1 or anchor.line_number > total_lines:
            raise ValidationError(
                f"line_number {anchor.line_number} out of range (1-{total_lines})"
            )
        if fm_range is not None and anchor.line_number <= fm_range.end_line:
            raise FrontmatterProtectedError(
                f"Cannot insert at line {anchor.line_number}; it is inside protected frontmatter"
            )
        _validate_anchor_hash(content, anchor.line_number, anchor.expected_anchor_sha256)
        return anchor.line_number - 1 if anchor.position == "before" else anchor.line_number

    if anchor.kind == "section":
        if anchor.section_id is None:
            raise ValidationError("section_id is required for a section anchor")
        section = resolve_section(content, anchor.section_id)
        if (
            anchor.expected_anchor_sha256 is not None
            and section.content_sha256 != anchor.expected_anchor_sha256
        ):
            raise TargetHashMismatchError(f"Section {anchor.section_id!r} changed since lookup")
        return section.start_line - 1 if anchor.position == "before" else section.end_line

    # match anchor
    line_number = resolve_match_anchor_line(
        content, anchor.match_text or "", anchor.match_occurrence
    )
    # Guard with the dual-hash model like line/section anchors: reject if the
    # matched line changed since the caller looked it up.
    _validate_anchor_hash(content, line_number, anchor.expected_anchor_sha256)
    return line_number - 1 if anchor.position == "before" else line_number


def prepare_insert_patch(content: str, anchor: InsertAnchor, insert_content: str) -> PreparedPatch:
    """Insert content before/after a line, section, match, or at end of file."""
    index = _resolve_insert_index(content, anchor)
    lines = split_document_lines(content)
    insert_lines = insert_content.split("\n")
    rebuilt = lines[:index] + insert_lines + lines[index:]
    new_full = "\n".join(rebuilt)
    finalized = _finalize(content, new_full)

    occurrence: JsonValue = anchor.match_occurrence
    target: JsonObject = {
        "kind": "insert",
        "anchor_kind": anchor.kind,
        "position": anchor.position,
        "insert_index": index,
        "line_number": anchor.line_number,
        "section_id": anchor.section_id,
        "match_text": anchor.match_text,
        "match_occurrence": occurrence,
        "insert_content_sha256": compute_sha256(insert_content),
    }
    return PreparedPatch(
        proposal_kind="insert",
        new_full_content=finalized,
        target_before_sha256=anchor.expected_anchor_sha256,
        target=target,
    )


def _match_not_found_details(content: str, needle: str) -> JsonObject:
    """Build recovery details for a failed exact/literal match."""
    details: JsonObject = {"current_match_count": 0}
    if has_whitespace_normalized_match(content, needle):
        details["hint"] = (
            "A whitespace-normalized version of the text matches: the document differs "
            "only in spaces/tabs/indentation. Re-read the exact lines and copy them verbatim."
        )
    else:
        details["hint"] = (
            "Matching is exact and case-sensitive, including whitespace and newlines. "
            "Use find_in_document or read_document_range to copy the current text verbatim."
        )
    return details


def _match_spans(matches: Sequence[ExactMatch]) -> list[JsonValue]:
    return [{"start_line": m.start_line, "end_line": m.end_line} for m in matches]


def _select_exact_match(
    content: str,
    old_string: str,
    occurrence: int | None,
    *,
    edit_index: int | None = None,
) -> ExactMatch:
    """Find *old_string* in *content* and select a single unambiguous match."""
    extra: JsonObject = {} if edit_index is None else {"edit_index": edit_index}
    matches = find_exact_matches(content, old_string)
    fm_range, _body_start = frontmatter_line_range(content)
    if fm_range is not None:
        body = [m for m in matches if m.start_line > fm_range.end_line]
        if matches and not body:
            raise FrontmatterProtectedError(
                "old_string only matches protected frontmatter; use propose_frontmatter_patch "
                "for metadata changes",
                details=extra or None,
            )
        matches = body
    if not matches:
        raise MatchNotFoundError(
            f"old_string not found: {old_string[:120]!r}",
            details={**_match_not_found_details(content, old_string), **extra},
        )
    if len(matches) > 1 and occurrence is None:
        raise AmbiguousMatchError(
            f"old_string matches {len(matches)} times; expand it with surrounding lines to "
            "make it unique, or pass occurrence to select one",
            details={
                "current_match_count": len(matches),
                "match_spans": _match_spans(matches),
                **extra,
            },
        )
    selected_index = (occurrence or 1) - 1
    if selected_index < 0 or selected_index >= len(matches):
        raise MatchNotFoundError(
            f"Occurrence {occurrence} out of range (found {len(matches)} matches)",
            details={"current_match_count": len(matches), **extra},
        )
    return matches[selected_index]


def prepare_exact_replace_patch(
    content: str,
    *,
    old_string: str,
    new_string: str,
    occurrence: int | Literal["all"] | None = None,
    expected_match_count: int | None = None,
) -> PreparedPatch:
    """Replace an exact (possibly multi-line) occurrence of ``old_string``.

    The matched text itself is the guard: matching is case-sensitive and exact
    including whitespace and newlines, and the match must be unique unless
    ``occurrence`` (or ``occurrence='all'`` with ``expected_match_count``)
    disambiguates. No document hash is required, though callers may still
    guard with one at the propose layer.
    """
    if not old_string:
        raise ValidationError("old_string must not be empty")
    if old_string == new_string:
        raise ValidationError("new_string must differ from old_string")

    if occurrence == "all":
        matches = find_exact_matches(content, old_string)
        fm_range, _body_start = frontmatter_line_range(content)
        if fm_range is not None:
            body = [m for m in matches if m.start_line > fm_range.end_line]
            if matches and not body:
                raise FrontmatterProtectedError(
                    "old_string only matches protected frontmatter; use "
                    "propose_frontmatter_patch for metadata changes"
                )
            matches = body
        if not matches:
            raise MatchNotFoundError(
                f"old_string not found: {old_string[:120]!r}",
                details=_match_not_found_details(content, old_string),
            )
        if expected_match_count is None:
            raise AmbiguousMatchError(
                "occurrence='all' requires expected_match_count to confirm the match total",
                details={
                    "current_match_count": len(matches),
                    "match_spans": _match_spans(matches),
                },
            )
        if len(matches) != expected_match_count:
            raise PatchConflictError(
                f"Expected {expected_match_count} matches but found {len(matches)}",
                details={
                    "expected_match_count": expected_match_count,
                    "current_match_count": len(matches),
                    "match_spans": _match_spans(matches),
                },
            )
        selected = matches
    else:
        selected = [_select_exact_match(content, old_string, occurrence)]

    new_full = content
    for match in sorted(selected, key=lambda m: m.start_offset, reverse=True):
        new_full = new_full[: match.start_offset] + new_string + new_full[match.end_offset :]
    finalized = _finalize(content, new_full)

    occurrence_value: JsonValue = occurrence
    target: JsonObject = {
        "kind": "exact_replace",
        "old_string": old_string,
        "new_string": new_string,
        "occurrence": occurrence_value,
        "match_count": len(selected),
        "match_spans": _match_spans(selected),
    }
    return PreparedPatch(
        proposal_kind="exact_replace",
        new_full_content=finalized,
        target_before_sha256=compute_sha256(old_string),
        target=target,
    )


def prepare_multi_edit_patch(content: str, *, edits: Sequence[ExactEdit]) -> PreparedPatch:
    """Apply a batch of exact-replace edits sequentially and atomically.

    Each edit is resolved against the intermediate content produced by the
    previous edits, exactly like running the exact-replace tool repeatedly —
    but validation is all-or-nothing: any failing edit aborts the whole batch
    with its ``edit_index`` in the error details, and nothing is recorded.
    """
    if not edits:
        raise ValidationError("edits must not be empty")
    if len(edits) > MAX_MULTI_EDITS:
        raise ValidationError(
            f"Too many edits ({len(edits)}); maximum is {MAX_MULTI_EDITS}",
            details={"max_edits": MAX_MULTI_EDITS},
        )

    working = content
    applied: list[JsonValue] = []
    for index, edit in enumerate(edits):
        if not edit.old_string:
            raise ValidationError(
                f"edits[{index}].old_string must not be empty",
                details={"edit_index": index},
            )
        if edit.old_string == edit.new_string:
            raise ValidationError(
                f"edits[{index}].new_string must differ from old_string",
                details={"edit_index": index},
            )
        match = _select_exact_match(working, edit.old_string, edit.occurrence, edit_index=index)
        working = working[: match.start_offset] + edit.new_string + working[match.end_offset :]
        applied.append(
            {
                "edit_index": index,
                "start_line": match.start_line,
                "end_line": match.end_line,
                "occurrence": edit.occurrence,
                "old_string": edit.old_string,
                "new_string": edit.new_string,
            }
        )
    finalized = _finalize(content, working)

    target: JsonObject = {
        "kind": "multi_edit",
        "edit_count": len(edits),
        "edits": applied,
    }
    return PreparedPatch(
        proposal_kind="multi_edit",
        new_full_content=finalized,
        target_before_sha256=None,
        target=target,
    )


def _validate_frontmatter_patch_keys(set_values: JsonObject, remove_keys: Sequence[str]) -> None:
    """Validate the requested key sets before parsing or rewriting content."""
    if not set_values and not remove_keys:
        raise ValidationError("Nothing to change: provide set values and/or remove keys")
    overlap = sorted(set(set_values) & set(remove_keys))
    if overlap:
        raise ValidationError(
            f"Keys cannot be both set and removed: {overlap}",
            details={"conflicting_keys": list(overlap)},
        )
    touched = sorted({*set_values, *remove_keys})
    protected = [key for key in touched if key in PROTECTED_FRONTMATTER_KEYS]
    if protected:
        raise FrontmatterProtectedError(
            f"Frontmatter keys {protected} are managed by Ferumind and cannot be edited",
            details={"protected_keys": list(protected)},
        )


def _validate_frontmatter_behavior_values(set_values: JsonObject) -> None:
    """Validate optional status and edit-policy values in a patch request."""
    if "status" in set_values:
        try:
            validate_status(str(set_values["status"]))
        except FrontmatterInvalidError as exc:
            raise ValidationError(str(exc)) from exc
    if "edit_policy" in set_values:
        try:
            validate_edit_policy(str(set_values["edit_policy"]))
        except FrontmatterInvalidError as exc:
            raise ValidationError(str(exc)) from exc


def _validate_frontmatter_description_change(
    content: str, set_values: JsonObject, remove_keys: Sequence[str]
) -> None:
    """Require a valid description after edits to managed frontmatter."""
    # ``description`` is required, not protected: an agent that notices a stale
    # one should be able to rewrite it through this ordinary flow. What it may
    # not do is leave the document without one, so a set value is validated and
    # a removal is refused outright on a managed document.
    if "description" in set_values:
        try:
            validate_description(set_values["description"])
        except FrontmatterInvalidError as exc:
            raise ValidationError(str(exc)) from exc
    if "description" in remove_keys and is_managed_markdown(content):
        raise ValidationError(
            "description is required on a managed document and cannot be removed; "
            "set a new one instead",
            details={"required_keys": ["description"]},
        )


def _remove_frontmatter_keys(
    frontmatter: JsonObject, set_values: JsonObject, remove_keys: Sequence[str]
) -> JsonObject:
    """Apply validated set/remove operations to a copied mapping."""
    missing = sorted(key for key in remove_keys if key not in frontmatter)
    if missing:
        raise ValidationError(
            f"Cannot remove frontmatter keys that do not exist: {missing}",
            details={"missing_keys": list(missing)},
        )

    new_frontmatter: JsonObject = dict(frontmatter)
    new_frontmatter.update(set_values)
    for key in remove_keys:
        del new_frontmatter[key]
    return new_frontmatter


def prepare_frontmatter_patch(
    content: str,
    *,
    set_values: JsonObject,
    remove_keys: Sequence[str],
) -> PreparedPatch:
    """Set or remove individual frontmatter keys, protecting managed identity keys.

    Rewrites the frontmatter block from the parsed mapping (YAML comments are
    not preserved). Identity/lineage keys (``id``/``type``/``project``/
    ``created``) and the automatic ``updated`` timestamp cannot be set or
    removed.
    """
    _validate_frontmatter_patch_keys(set_values, remove_keys)
    fm_block, body = extract_frontmatter_block(content)
    if not fm_block:
        raise FrontmatterRequiredError("Document has no frontmatter block to edit")
    frontmatter = parse_frontmatter(content)
    _validate_frontmatter_behavior_values(set_values)
    _validate_frontmatter_description_change(content, set_values, remove_keys)
    new_frontmatter = _remove_frontmatter_keys(frontmatter, set_values, remove_keys)

    dumped = yaml.safe_dump(
        new_frontmatter, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    new_full = f"---\n{dumped}---\n{body}"
    finalized = _finalize(content, new_full)

    # list[str] → list[JsonValue] is safe (str is a JsonValue); cast for invariance.
    set_keys = cast(list[JsonValue], sorted(set_values))
    removed_keys = cast(list[JsonValue], sorted(remove_keys))
    target: JsonObject = {
        "kind": "frontmatter",
        "set_values": set_values,
        "set_keys": set_keys,
        "removed_keys": removed_keys,
    }
    return PreparedPatch(
        proposal_kind="frontmatter",
        new_full_content=finalized,
        target_before_sha256=compute_sha256(fm_block),
        target=target,
    )
