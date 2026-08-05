"""YAML frontmatter parsing, generation, and the v2 behavioral keys.

Frontmatter v2 (product/spec-mcp.md §3): identity keys ``id``/``type``/
``project``/``created`` plus the automatic ``updated`` are protected;
behavior rides on ``status`` (active|gated|frozen|archived) and the optional
``edit_policy`` (free|append|propose-first|ask-human) with folder defaults.
``type`` is always ``document``.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Final, cast

import yaml

from ferumind.core.errors import FrontmatterInvalidError
from ferumind.core.types import JsonObject, JsonValue
from ferumind.core.yaml_safe import safe_load_yaml

DOCUMENT_TYPE: Final = "document"

REQUIRED_FRONTMATTER_KEYS: Final[tuple[str, ...]] = ("id", "type", "project")

#: Keys owned by Ferumind: identity/lineage keys are immutable and ``updated``
#: is maintained automatically on every applied patch.
PROTECTED_FRONTMATTER_KEYS: Final[tuple[str, ...]] = ("id", "type", "project", "created", "updated")

ALLOWED_STATUSES: Final[frozenset[str]] = frozenset({"active", "gated", "frozen", "archived"})
ALLOWED_EDIT_POLICIES: Final[frozenset[str]] = frozenset(
    {"free", "append", "propose-first", "ask-human"}
)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n?", re.DOTALL)
_FRONTMATTER_OPEN_RE = re.compile(r"^---[ \t]*\r?\n")
MAX_FRONTMATTER_BYTES: Final = 64 * 1024


def _match_frontmatter(content: str) -> re.Match[str] | None:
    match = _FRONTMATTER_RE.match(content)
    if match is None and _FRONTMATTER_OPEN_RE.match(content):
        raise FrontmatterInvalidError("Frontmatter opening delimiter is not terminated")
    return match


def parse_frontmatter(content: str) -> JsonObject:
    """Extract YAML frontmatter, failing closed when a present block is invalid."""
    match = _match_frontmatter(content)
    if not match:
        return {}
    try:
        result = safe_load_yaml(match.group(1), max_bytes=MAX_FRONTMATTER_BYTES)
    except yaml.YAMLError as exc:
        raise FrontmatterInvalidError("Frontmatter is invalid YAML") from exc
    if not isinstance(result, dict):
        raise FrontmatterInvalidError("Frontmatter must be a YAML mapping")
    result_dict = cast(dict[object, object], result)
    return {str(k): _to_json_value(v) for k, v in result_dict.items()}


def _to_json_value(value: object) -> JsonValue:
    if isinstance(value, dict):
        value_dict = cast(dict[object, object], value)
        return {str(k): _to_json_value(v) for k, v in value_dict.items()}
    if isinstance(value, list):
        value_list = cast(list[object], value)
        return [_to_json_value(v) for v in value_list]
    if isinstance(value, str | int | float | bool):
        return value
    if value is None:
        return None
    return str(value)


def extract_frontmatter_block(content: str) -> tuple[str, str]:
    """Split content into (frontmatter_block, body); the block includes delimiters."""
    match = _match_frontmatter(content)
    if not match:
        return "", content
    return match.group(0), content[match.end() :]


def new_document_id() -> str:
    return f"doc_{uuid.uuid4().hex[:12]}"


def validate_status(status: str) -> str:
    if status not in ALLOWED_STATUSES:
        msg = f"Invalid status {status!r}: must be one of {sorted(ALLOWED_STATUSES)}"
        raise FrontmatterInvalidError(msg)
    return status


def validate_edit_policy(edit_policy: str) -> str:
    if edit_policy not in ALLOWED_EDIT_POLICIES:
        msg = f"Invalid edit_policy {edit_policy!r}: must be one of {sorted(ALLOWED_EDIT_POLICIES)}"
        raise FrontmatterInvalidError(msg)
    return edit_policy


def generate_frontmatter(
    *,
    doc_id: str,
    project_key: str,
    title: str,
    status: str = "active",
    edit_policy: str | None = None,
) -> str:
    """Generate the YAML frontmatter block for a new v2 document."""
    validate_status(status)
    if edit_policy is not None:
        validate_edit_policy(edit_policy)
    now = datetime.now(UTC).isoformat()
    payload: dict[str, str] = {
        "id": doc_id,
        "type": DOCUMENT_TYPE,
        "project": project_key,
        "title": title,
        "status": status,
    }
    if edit_policy is not None:
        payload["edit_policy"] = edit_policy
    payload["created"] = now
    payload["updated"] = now
    dumped = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return f"---\n{dumped}---\n"


def is_managed_markdown(content: str) -> bool:
    """Return ``True`` when *content* carries required id/type/project frontmatter."""
    fm = parse_frontmatter(content)
    return all(
        isinstance(fm.get(key), str) and str(fm.get(key)).strip()
        for key in REQUIRED_FRONTMATTER_KEYS
    )


def set_frontmatter_updated(fm_block: str, now: str) -> str:
    """Set the ``updated`` field within a frontmatter block (delimiters included)."""
    updated_pattern = re.compile(r"^updated\s*:.*$", re.MULTILINE)
    if updated_pattern.search(fm_block):
        return updated_pattern.sub(f"updated: {now}", fm_block, count=1)
    lines = fm_block.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "---":
            lines.insert(i, f"updated: {now}")
            break
    return "\n".join(lines)


def refresh_updated_if_managed(content: str) -> str:
    """Refresh the ``updated`` timestamp when *content* is a managed document."""
    if not is_managed_markdown(content):
        return content
    fm_block, body = extract_frontmatter_block(content)
    if not fm_block:
        return content
    now = datetime.now(UTC).isoformat()
    return set_frontmatter_updated(fm_block, now) + body


def infer_title(frontmatter: JsonObject, body: str, path: str) -> str:
    """Infer a document title: frontmatter title > first H1 > filename slug."""
    title_val = frontmatter.get("title")
    if isinstance(title_val, str) and title_val.strip():
        return title_val.strip()
    h1_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
    if h1_match:
        return h1_match.group(1).strip()
    stem = path.rsplit("/", 1)[-1].removesuffix(".md")
    return stem.replace("-", " ").replace("_", " ").title().strip()
