"""One structural Markdown-link extractor for lint and future link readers.

The extractor is deliberately small and deterministic rather than a rendering
engine.  It recognizes the CommonMark link forms Ferumind needs for path
resolution, suppresses code, and preserves use-site line numbers.  It does
not fetch, normalize, or resolve destinations; callers own those boundaries.
"""

from __future__ import annotations

import html
import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Final, Literal

LinkKind = Literal["link", "image"]

_FENCE_OPEN_RE: Final = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_REFERENCE_DEFINITION_RE: Final = re.compile(r"^ {0,3}\[([^\]\n]+)\]:[ \t]*(.*)$")
_AUTOLINK_SCHEME_RE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]{1,31}:[^ <>]*$")
_AUTOLINK_EMAIL_RE: Final = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
_ESCAPABLE_RE: Final = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])")
_BLANK_LINE_RE: Final = re.compile(r"\r?\n[ \t]*\r?\n")
_LIST_MARKER_RE: Final = re.compile(r"(?:[*+-]|\d{1,9}[.)])(?=[ \t])")


@dataclass(frozen=True, slots=True)
class MarkdownLink:
    """One Markdown link or image at its source location."""

    destination: str
    line: int
    kind: LinkKind


@dataclass(frozen=True, slots=True)
class _ReferenceDefinitions:
    destinations: dict[str, str]
    spans: tuple[tuple[int, int], ...]


def extract_markdown_links(markdown: str) -> list[MarkdownLink]:
    """Return links and images in source order, excluding fenced/inline code.

    Inline, full/collapsed/shortcut reference links, images, multiline inline
    links, and URI/email autolinks are recognized.  Bare URLs are ordinary
    text and are intentionally ignored.
    """
    visible = _mask_code(markdown)
    references = _reference_definitions(visible)
    newlines = [index for index, character in enumerate(visible) if character == "\n"]
    links: list[MarkdownLink] = []
    span_index = 0
    index = 0
    while index < len(visible):
        while span_index < len(references.spans) and index >= references.spans[span_index][1]:
            span_index += 1
        if span_index < len(references.spans):
            start, end = references.spans[span_index]
            if start <= index < end:
                index = end
                continue

        parsed = _links_at(visible, index, references.destinations, newlines)
        if parsed is None:
            index += 1
            continue
        found, index = parsed
        links.extend(found)
    return links


def _links_at(
    text: str,
    index: int,
    references: dict[str, str],
    newlines: list[int],
) -> tuple[list[MarkdownLink], int] | None:
    character = text[index]
    if character == "<" and not _is_escaped(text, index):
        autolink = _parse_autolink(text, index)
        if autolink is not None:
            destination, end = autolink
            return [MarkdownLink(destination, _line_at(newlines, index), "link")], end

    image = (
        character == "!"
        and not _is_escaped(text, index)
        and index + 1 < len(text)
        and text[index + 1] == "["
    )
    open_bracket = index + 1 if image else index
    follows_image_marker = (
        not image
        and open_bracket > 0
        and text[open_bracket - 1] == "!"
        and not _is_escaped(text, open_bracket - 1)
    )
    if text[open_bracket] != "[" or _is_escaped(text, open_bracket) or follows_image_marker:
        return None
    parsed = _parse_bracket_link(
        text,
        open_bracket,
        references,
        reject_nested_links=not image,
    )
    if parsed is None:
        return None

    destination, end = parsed
    found = [
        MarkdownLink(
            destination=destination,
            line=_line_at(newlines, index),
            kind="image" if image else "link",
        )
    ]
    if not image:
        close_bracket = _matching_bracket(text, open_bracket)
        if close_bracket is not None:
            found.extend(
                _nested_images(
                    text,
                    open_bracket + 1,
                    close_bracket,
                    references,
                    newlines,
                )
            )
    return found, end


def _nested_images(
    text: str,
    start: int,
    end: int,
    references: dict[str, str],
    newlines: list[int],
) -> list[MarkdownLink]:
    """Extract images nested in a link label without admitting nested links."""
    images: list[MarkdownLink] = []
    index = start
    while index < end:
        if (
            text[index] != "!"
            or _is_escaped(text, index)
            or index + 1 >= end
            or text[index + 1] != "["
        ):
            index += 1
            continue
        parsed = _parse_bracket_link(
            text,
            index + 1,
            references,
            reject_nested_links=False,
        )
        if parsed is None or parsed[1] > end:
            index += 1
            continue
        destination, parsed_end = parsed
        images.append(MarkdownLink(destination, _line_at(newlines, index), "image"))
        index = parsed_end
    return images


def _mask_code(markdown: str) -> str:
    fenced = _mask_fenced_code(markdown)
    indented = _mask_indented_code(fenced)
    return _mask_inline_code(indented)


@dataclass(frozen=True, slots=True)
class _ContainerLine:
    content: str
    prefix_length: int
    continuation_indent: int


def _mask_fenced_code(markdown: str) -> str:
    output: list[str] = []
    marker: str | None = None
    marker_length = 0
    container_indent = 0
    for line in markdown.splitlines(keepends=True):
        raw_content = line.rstrip("\r\n")
        container = _strip_container_prefix(raw_content)
        if marker is None:
            match = _FENCE_OPEN_RE.match(container.content)
            if match is None:
                output.append(line)
                continue
            # CommonMark forbids backticks in a backtick fence's info string.
            # Treating such a line as a fence would hide real links until EOF.
            if match.group(2).startswith("`") and "`" in match.group(3):
                output.append(line)
                continue
            marker = match.group(2)[0]
            marker_length = len(match.group(2))
            container_indent = container.continuation_indent
            output.append(_blank(line))
            continue

        # A nonblank line escaping a list container closes its unclosed fence.
        # A block quote is handled the same way by ``continuation_indent == 0``
        # and the prefix-stripped content below.
        if (
            container_indent > 0
            and raw_content.strip()
            and _leading_columns(raw_content) < container_indent
            and container.prefix_length == 0
        ):
            marker = None
            marker_length = 0
            container_indent = 0
            output.append(line)
            continue

        output.append(_blank(line))
        stripped = container.content.lstrip(" ")
        if len(container.content) - len(stripped) > 3:
            continue
        run = len(stripped) - len(stripped.lstrip(marker))
        if run >= marker_length and not stripped[run:].strip():
            marker = None
            marker_length = 0
            container_indent = 0
    return "".join(output)


def _strip_container_prefix(content: str) -> _ContainerLine:
    """Strip leading CommonMark quote/list markers for block recognition."""
    index = 0
    continuation_indent = 0
    while True:
        start = index
        spaces = _up_to_three_spaces(content, index)
        cursor = index + spaces
        if cursor < len(content) and content[cursor] == ">":
            cursor += 1
            if cursor < len(content) and content[cursor] in " \t":
                cursor += 1
            index = cursor
            continue
        marker = _LIST_MARKER_RE.match(content, cursor)
        if marker is not None:
            whitespace_end = marker.end()
            while whitespace_end < len(content) and content[whitespace_end] in " \t":
                whitespace_end += 1
            width = whitespace_end - marker.end()
            padding = width if 1 <= width <= 4 else 1
            continuation_indent += spaces + len(marker.group()) + padding
            index = marker.end() + padding
            continue
        if start == index:
            break
        break
    return _ContainerLine(content[index:], index, continuation_indent)


def _mask_indented_code(markdown: str) -> str:
    output: list[str] = []
    paragraph_open = False
    in_indented_code = False
    for line in markdown.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        container = _strip_container_prefix(content)
        indent = _leading_columns(container.content)
        blank = not container.content.strip()
        if in_indented_code:
            if blank or indent >= 4:
                output.append(_blank(line))
                continue
            in_indented_code = False
        if indent >= 4 and not paragraph_open:
            in_indented_code = True
            output.append(_blank(line))
            continue
        output.append(line)
        if blank:
            paragraph_open = False
        elif indent < 4:
            paragraph_open = _can_continue_paragraph(container.content)
    return "".join(output)


def _can_continue_paragraph(content: str) -> bool:
    stripped = content.lstrip(" ")
    return not (
        not stripped
        or stripped.startswith(("#", "```", "~~~"))
        or _LIST_MARKER_RE.match(stripped) is not None
    )


def _up_to_three_spaces(text: str, start: int) -> int:
    end = start
    while end < len(text) and end - start < 3 and text[end] == " ":
        end += 1
    return end - start


def _leading_columns(text: str) -> int:
    columns = 0
    for character in text:
        if character == " ":
            columns += 1
        elif character == "\t":
            columns += 4 - (columns % 4)
        else:
            break
    return columns


def _mask_inline_code(markdown: str) -> str:
    characters = list(markdown)
    index = 0
    while index < len(markdown):
        if markdown[index] != "`":
            index += 1
            continue
        if _is_escaped(markdown, index):
            index += 1
            continue
        run = _run_length(markdown, index, "`")
        close = _matching_backtick_run(markdown, index + run, run)
        if close is None:
            index += run
            continue
        for position in range(index, close + run):
            if characters[position] not in "\r\n":
                characters[position] = " "
        index = close + run
    return "".join(characters)


def _matching_backtick_run(markdown: str, start: int, length: int) -> int | None:
    index = start
    while index < len(markdown):
        found = markdown.find("`", index)
        if found < 0:
            return None
        run = _run_length(markdown, found, "`")
        if run == length:
            return found
        index = found + run
    return None


def _run_length(text: str, start: int, character: str) -> int:
    end = start
    while end < len(text) and text[end] == character:
        end += 1
    return end - start


def _blank(text: str) -> str:
    return "".join(character if character in "\r\n" else " " for character in text)


def _reference_definitions(markdown: str) -> _ReferenceDefinitions:
    destinations: dict[str, str] = {}
    spans: list[tuple[int, int]] = []
    offset = 0
    for line in markdown.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        match = _REFERENCE_DEFINITION_RE.match(content)
        if match is not None:
            destination = _definition_destination(match.group(2))
            label = _normalize_label(match.group(1))
            if destination is not None and label:
                # The first definition wins, but every syntactically valid
                # definition is block syntax and must be suppressed as such.
                destinations.setdefault(label, destination)
                spans.append((offset, offset + len(line)))
        offset += len(line)
    return _ReferenceDefinitions(destinations, tuple(spans))


def _definition_destination(remainder: str) -> str | None:
    stripped = remainder.lstrip()
    if not stripped:
        return None
    parsed = (
        _angle_destination(stripped) if stripped.startswith("<") else _bare_destination(stripped)
    )
    if parsed is None:
        return None
    destination, destination_end = parsed
    suffix = stripped[destination_end:]
    if not suffix:
        return destination
    if not suffix[0].isspace() or _BLANK_LINE_RE.search(suffix):
        return None
    title = suffix.strip()
    if not _valid_optional_title(title):
        return None
    return destination


def _angle_destination(text: str) -> tuple[str, int] | None:
    close = _find_unescaped(text, ">", 1)
    if close is None:
        return None
    raw_destination = text[1:close]
    if "\n" in raw_destination or "\r" in raw_destination or "<" in raw_destination:
        return None
    return _normalize_destination(raw_destination), close + 1


def _bare_destination(text: str) -> tuple[str, int] | None:
    end = _bare_destination_end(text)
    if end is None:
        return None
    return _normalize_destination(text[:end]), end


def _bare_destination_end(text: str) -> int | None:
    """Return the end of one balanced, non-whitespace bare destination."""
    end = 0
    depth = 0
    while end < len(text):
        character = text[end]
        if character.isspace() and depth == 0:
            break
        if character == "(" and not _is_escaped(text, end):
            depth += 1
        elif character == ")" and not _is_escaped(text, end):
            if depth == 0:
                break
            depth -= 1
        end += 1
    return end if end and depth == 0 else None


def _valid_optional_title(title: str) -> bool:
    if len(title) < 2:
        return False
    closer = {'"': '"', "'": "'", "(": ")"}.get(title[0])
    if closer is None:
        return False
    close = _find_unescaped(title, closer, 1)
    return close == len(title) - 1


def _parse_autolink(text: str, start: int) -> tuple[str, int] | None:
    close = text.find(">", start + 1)
    if close < 0:
        return None
    value = text[start + 1 : close]
    if "\n" in value or "\r" in value:
        return None
    if _AUTOLINK_SCHEME_RE.fullmatch(value):
        return _normalize_destination(value), close + 1
    if _AUTOLINK_EMAIL_RE.fullmatch(value):
        return f"mailto:{_normalize_destination(value)}", close + 1
    return None


def _parse_bracket_link(
    text: str,
    open_bracket: int,
    references: dict[str, str],
    *,
    reject_nested_links: bool,
) -> tuple[str, int] | None:
    close_bracket = _matching_bracket(text, open_bracket)
    if close_bracket is None:
        return None
    label_text = text[open_bracket + 1 : close_bracket]
    invalid_label = _BLANK_LINE_RE.search(label_text) is not None or (
        reject_nested_links
        and _contains_link(
            text,
            open_bracket + 1,
            close_bracket,
            references,
        )
    )
    if invalid_label:
        return None
    return _destination_after_label(text, close_bracket, label_text, references)


def _destination_after_label(
    text: str,
    close_bracket: int,
    label_text: str,
    references: dict[str, str],
) -> tuple[str, int] | None:
    following = close_bracket + 1
    if following < len(text) and text[following] == "(":
        inline = _inline_destination(text, following)
        if inline is not None:
            return inline
    if following < len(text) and text[following] == "[":
        reference_close = _find_unescaped(text, "]", following + 1)
        if reference_close is None:
            return None
        explicit = text[following + 1 : reference_close]
        reference_label = explicit if explicit else label_text
        if _BLANK_LINE_RE.search(reference_label):
            return None
        destination = references.get(_normalize_label(reference_label))
        return (destination, reference_close + 1) if destination is not None else None
    destination = references.get(_normalize_label(label_text))
    return (destination, close_bracket + 1) if destination is not None else None


def _contains_link(
    text: str,
    start: int,
    end: int,
    references: dict[str, str],
) -> bool:
    """Return whether a label contains a link, which invalidates an outer link."""
    index = start
    while index < end:
        if text[index] != "[" or _is_escaped(text, index):
            index += 1
            continue
        if index > start and text[index - 1] == "!" and not _is_escaped(text, index - 1):
            image = _parse_bracket_link(
                text,
                index,
                references,
                reject_nested_links=False,
            )
            index = image[1] if image is not None and image[1] <= end else index + 1
            continue
        nested = _parse_bracket_link(
            text,
            index,
            references,
            # The caller only needs to know that a nested link shape exists.
            # Avoid recursive validation here: the outer link is invalid in
            # either case, and iterative scanning will find the innermost use.
            reject_nested_links=False,
        )
        if nested is not None and nested[1] <= end:
            return True
        index += 1
    return False


def _matching_bracket(text: str, opening: int) -> int | None:
    depth = 0
    index = opening
    while index < len(text):
        character = text[index]
        if character == "[" and not _is_escaped(text, index):
            depth += 1
        elif character == "]" and not _is_escaped(text, index):
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _inline_destination(text: str, opening: int) -> tuple[str, int] | None:
    closing = _matching_parenthesis(text, opening)
    if closing is None:
        return None
    interior = text[opening + 1 : closing].strip()
    if _BLANK_LINE_RE.search(text[opening + 1 : closing]):
        return None
    destination = _definition_destination(interior)
    if destination is None:
        # ``[]()`` is a valid empty destination: it names the current document.
        destination = "" if not interior else None
    return (destination, closing + 1) if destination is not None else None


def _matching_parenthesis(text: str, opening: int) -> int | None:
    depth = 0
    in_angle = False
    index = opening
    while index < len(text):
        character = text[index]
        if _is_escaped(text, index):
            index += 1
            continue
        if character == "<" and depth == 1:
            in_angle = True
        elif character == ">" and in_angle:
            in_angle = False
        elif not in_angle and character == "(":
            depth += 1
        elif not in_angle and character == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _find_unescaped(text: str, character: str, start: int) -> int | None:
    index = start
    while index < len(text):
        found = text.find(character, index)
        if found < 0:
            return None
        if not _is_escaped(text, found):
            return found
        index = found + 1
    return None


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _normalize_label(label: str) -> str:
    unescaped = _normalize_destination(label)
    return " ".join(unescaped.split()).casefold()


def _normalize_destination(destination: str) -> str:
    return html.unescape(_ESCAPABLE_RE.sub(r"\1", destination.strip()))


def _line_at(newlines: list[int], index: int) -> int:
    return bisect_right(newlines, index - 1) + 1


__all__ = ["LinkKind", "MarkdownLink", "extract_markdown_links"]
