"""Workspace-level compact files.

Compacts are not project documents. They live under ``workspace/compacts/``
and preserve handoff summaries for chats that do not belong to a project.
The server stores and audits them mechanically; the chat agent does the
distillation work.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_core import ValidationError as PydanticValidationError

from lattice.core.documents import compute_sha256
from lattice.core.errors import (
    CompactArchivedError,
    CompactIntegrityError,
    CompactNotFoundError,
    FileTooLargeError,
    FrontmatterInvalidError,
    ValidationError,
)
from lattice.core.file_io import atomic_write_text
from lattice.core.frontmatter import extract_frontmatter_block
from lattice.core.locks import acquire_workspace_lock
from lattice.core.operations import OP_APPLIED, WORKSPACE_OPERATION_PROJECT, record_operation
from lattice.core.paths import WorkspaceRoot, assert_no_symlink_escape, contained_path
from lattice.core.registry import ProjectEntry, validate_project_key
from lattice.core.security import assert_not_symlink
from lattice.core.snapshots import create_global_snapshot, new_snapshot_id, record_snapshot_in_db
from lattice.core.types import DbConnection, JsonObject
from lattice.core.yaml_safe import safe_load_yaml

COMPACTS_DIR: Final = "compacts"
TOKEN_RE: Final = re.compile(r"^[a-z]+-[a-z]+-[a-z]+-[a-z]+$")
COMPACT_FILENAME_RE: Final = re.compile(r"^compact_([a-z]+-[a-z]+-[a-z]+-[a-z]+)\.md$")
MAX_COMPACT_BYTES: Final = 5 * 1024 * 1024
MAX_COMPACT_ITEMS: Final = 200
MAX_COMPACT_ITEM_CHARS: Final = 4096
MAX_COMPACT_LIST_LIMIT: Final = 200
MAX_COMPACT_TOKEN_CHARS: Final = 128
MAX_HANDOFF_PROMPT_CHARS: Final = 64 * 1024

type CompactState = Literal["draft", "finalized", "resumed", "archived", "stale"]

ALLOWED_COMPACT_STATES: Final[frozenset[str]] = frozenset(
    {"draft", "finalized", "resumed", "archived", "stale"}
)

_TOKEN_WORDS: Final[tuple[str, ...]] = (
    "amber",
    "anchor",
    "atlas",
    "basil",
    "beacon",
    "birch",
    "bravo",
    "cactus",
    "canyon",
    "cedar",
    "cinder",
    "clover",
    "cobalt",
    "coral",
    "delta",
    "ember",
    "falcon",
    "fennel",
    "field",
    "fjord",
    "flint",
    "forest",
    "harbor",
    "hazel",
    "indigo",
    "juniper",
    "lagoon",
    "lantern",
    "laurel",
    "linen",
    "maple",
    "meadow",
    "mesa",
    "mint",
    "north",
    "olive",
    "onyx",
    "orchid",
    "pebble",
    "pine",
    "prairie",
    "quartz",
    "raven",
    "ridge",
    "river",
    "saffron",
    "sage",
    "signal",
    "silver",
    "solace",
    "summit",
    "tango",
    "thistle",
    "timber",
    "topaz",
    "valley",
    "violet",
    "willow",
    "winter",
    "zephyr",
)


class CompactFrontmatter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=MAX_COMPACT_TOKEN_CHARS)
    created: str = Field(min_length=1, max_length=64)
    updated: str = Field(min_length=1, max_length=64)
    project: str | None = Field(default=None, max_length=64)
    state: CompactState
    resume_count: int = Field(default=0, ge=0)
    handoff_prompt: str | None = Field(default=None, max_length=MAX_HANDOFF_PROMPT_CHARS)
    sources: list[str] = Field(default_factory=list, max_length=MAX_COMPACT_ITEMS)
    tags: list[str] = Field(default_factory=list, max_length=MAX_COMPACT_ITEMS)
    document_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CompactReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    path: str
    frontmatter: CompactFrontmatter
    body: str
    current_body_sha256: str
    integrity_ok: bool


class CompactListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    path: str
    project: str | None
    state: CompactState
    resume_count: int
    created: str
    updated: str
    sources: list[str]
    tags: list[str]
    document_sha256: str | None


class CompactMutationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    path: str
    operation_id: str
    snapshot_id: str
    state: CompactState
    document_sha256: str | None


class CompactResumeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    path: str
    operation_id: str
    snapshot_id: str
    state: CompactState
    resume_count: int
    handoff_prompt: str
    body: str
    document_sha256: str | None


def compact_instructions() -> str:
    """Return the model-facing compacting procedure."""
    return """Use this only when the user explicitly invokes `/compact`, `@lattice /compact`, or asks for a Lattice compact.

The Lattice server will not summarize the chat. You, the chat agent, must:

1. Gather the visible thread and any referenced source material available to you.
2. Distill the material yourself. For long chats, create a compact draft, summarize chunks, append each chunk, then finalize.
3. Redact or omit secrets before writing. The server does not scan for secrets.
4. Finalize with Markdown whose first body block is exactly:

## Handoff Prompt

<the mandatory prompt the next chat must follow>

5. After that block, write: Short TL;DR, Key decisions / facts, Open questions / constraints, Actions / next steps, Relevant excerpts with provenance, Full distilled timeline / summary, and optional machine-readable sources.
6. Use ordinary project memory or document tools instead when the user wants to save project facts, notes, decisions, or summaries without explicitly asking for a Lattice compact.
"""


def new_compact_token(words: Sequence[str] = _TOKEN_WORDS) -> str:
    """Return a four-word human-readable token."""
    if len(words) < 4:
        raise ValidationError("At least four token words are required")
    return "-".join(secrets.choice(words) for _ in range(4))


def compact_relative_path(token: str) -> str:
    token = validate_compact_token(token)
    return f"{COMPACTS_DIR}/compact_{token}.md"


def validate_compact_token(token: str) -> str:
    normalized = token.strip().lower()
    if len(normalized) > MAX_COMPACT_TOKEN_CHARS:
        raise ValidationError(
            f"compact token exceeds the {MAX_COMPACT_TOKEN_CHARS}-character limit"
        )
    if not TOKEN_RE.fullmatch(normalized):
        raise ValidationError(
            "compact token must be four lowercase words separated by hyphens",
            details={"token": token},
        )
    return normalized


def create_compact_draft(
    conn: DbConnection,
    workspace_root: WorkspaceRoot,
    *,
    project: ProjectEntry | None = None,
    sources: Sequence[str] = (),
    tags: Sequence[str] = (),
) -> CompactMutationResult:
    with acquire_workspace_lock(workspace_root):
        compacts_dir = _ensure_compacts_dir(workspace_root)
        token = _unique_token(compacts_dir)
        rel_path = compact_relative_path(token)
        target = _compact_path(workspace_root, token)
        now = _now()
        frontmatter = CompactFrontmatter(
            id=token,
            created=now,
            updated=now,
            project=project.key if project is not None else None,
            state="draft",
            resume_count=0,
            handoff_prompt=None,
            sources=_dedupe(sources),
            tags=_dedupe(tags),
            document_sha256=None,
        )
        content = _render_compact(frontmatter, "## Draft Chunks\n")
        return _record_compact_mutation(
            conn,
            workspace_root,
            target=target,
            operation_type="create_compact_draft",
            rel_path=rel_path,
            before_content=None,
            after_content=content,
            request_json={
                "project": frontmatter.project,
                "sources_count": len(frontmatter.sources),
                "tags_count": len(frontmatter.tags),
            },
            token=token,
            state=frontmatter.state,
            document_sha256=frontmatter.document_sha256,
        )


def append_compact_chunk(
    conn: DbConnection,
    workspace_root: WorkspaceRoot,
    *,
    token: str,
    chunk_markdown: str,
    sources: Sequence[str] = (),
) -> CompactMutationResult:
    chunk = chunk_markdown.strip()
    if not chunk:
        raise ValidationError("chunk_markdown must not be empty")
    with acquire_workspace_lock(workspace_root):
        rel_path, target, before_content, frontmatter, body = _load_for_mutation(
            workspace_root, token
        )
        _ensure_not_archived(frontmatter)
        if frontmatter.state != "draft":
            raise ValidationError("Chunks can only be appended to draft compacts")
        frontmatter.sources = _dedupe([*frontmatter.sources, *sources])
        frontmatter.updated = _now()
        new_body = f"{body.rstrip()}\n\n## Chunk Summary\n\n{chunk}\n"
        after_content = _render_compact(frontmatter, new_body)
        return _record_compact_mutation(
            conn,
            workspace_root,
            target=target,
            operation_type="append_compact_chunk",
            rel_path=rel_path,
            before_content=before_content,
            after_content=after_content,
            request_json={
                "token": frontmatter.id,
                "chunk_bytes": len(chunk.encode("utf-8")),
                "sources_count": len(sources),
            },
            token=frontmatter.id,
            state=frontmatter.state,
            document_sha256=frontmatter.document_sha256,
        )


def finalize_compact(
    conn: DbConnection,
    workspace_root: WorkspaceRoot,
    *,
    token: str,
    handoff_prompt: str,
    final_markdown: str,
    sources: Sequence[str] = (),
    tags: Sequence[str] = (),
) -> CompactMutationResult:
    prompt = handoff_prompt.strip()
    body = final_markdown.strip()
    if not prompt:
        raise ValidationError("handoff_prompt must not be empty")
    if not body:
        raise ValidationError("final_markdown must not be empty")
    if not _starts_with_handoff(body, prompt):
        raise ValidationError(
            "final_markdown must start with a Handoff Prompt block containing handoff_prompt"
        )
    with acquire_workspace_lock(workspace_root):
        rel_path, target, before_content, frontmatter, _old_body = _load_for_mutation(
            workspace_root, token
        )
        _ensure_not_archived(frontmatter)
        if frontmatter.state != "draft":
            raise ValidationError("Only draft compacts can be finalized")
        frontmatter.handoff_prompt = prompt
        frontmatter.sources = _dedupe([*frontmatter.sources, *sources])
        frontmatter.tags = _dedupe([*frontmatter.tags, *tags])
        frontmatter.state = "finalized"
        frontmatter.updated = _now()
        final_body = f"{body}\n"
        frontmatter.document_sha256 = compute_sha256(final_body)
        after_content = _render_compact(frontmatter, final_body)
        return _record_compact_mutation(
            conn,
            workspace_root,
            target=target,
            operation_type="finalize_compact",
            rel_path=rel_path,
            before_content=before_content,
            after_content=after_content,
            request_json={
                "token": frontmatter.id,
                "sources_count": len(sources),
                "tags_count": len(tags),
                "body_bytes": len(body.encode("utf-8")),
            },
            token=frontmatter.id,
            state=frontmatter.state,
            document_sha256=frontmatter.document_sha256,
        )


def reseal_compact(
    conn: DbConnection,
    workspace_root: WorkspaceRoot,
    *,
    token: str,
) -> CompactMutationResult:
    """Accept the current compact body as intentional and refresh its hash.

    This is for deliberate out-of-band edits by the human. It does not alter
    the body, but it snapshots/logs the frontmatter update and marks the
    compact finalized so later resumes can trust the current content.
    """
    with acquire_workspace_lock(workspace_root):
        rel_path, target, before_content, frontmatter, body = _load_for_mutation(
            workspace_root, token
        )
        _ensure_not_archived(frontmatter)
        if frontmatter.handoff_prompt is None or not frontmatter.handoff_prompt.strip():
            raise ValidationError("Cannot reseal a compact without a handoff_prompt")
        if not _starts_with_handoff(body, frontmatter.handoff_prompt):
            raise ValidationError("Cannot reseal: body must start with the Handoff Prompt block")
        frontmatter.state = "finalized"
        frontmatter.updated = _now()
        frontmatter.document_sha256 = compute_sha256(body)
        after_content = _render_compact(frontmatter, body)
        return _record_compact_mutation(
            conn,
            workspace_root,
            target=target,
            operation_type="reseal_compact",
            rel_path=rel_path,
            before_content=before_content,
            after_content=after_content,
            request_json={"token": frontmatter.id},
            token=frontmatter.id,
            state=frontmatter.state,
            document_sha256=frontmatter.document_sha256,
        )


def read_compact(workspace_root: WorkspaceRoot, *, token: str) -> CompactReadResult:
    normalized = validate_compact_token(token)
    rel_path, target = _existing_compact_path(workspace_root, normalized)
    if target.stat().st_size > MAX_COMPACT_BYTES:
        raise FileTooLargeError(
            f"Compact exceeds the {MAX_COMPACT_BYTES}-byte safety limit",
            details={"max_bytes": MAX_COMPACT_BYTES, "scope": "compact"},
        )
    content = target.read_text(encoding="utf-8")
    frontmatter, body = parse_compact(content)
    if frontmatter.id != normalized:
        raise FrontmatterInvalidError("Compact id does not match its filename token")
    current_sha = compute_sha256(body)
    return CompactReadResult(
        token=frontmatter.id,
        path=rel_path,
        frontmatter=frontmatter,
        body=body,
        current_body_sha256=current_sha,
        integrity_ok=frontmatter.document_sha256 in (None, current_sha),
    )


def resume_compact(
    conn: DbConnection,
    workspace_root: WorkspaceRoot,
    *,
    token: str,
    auto_archive_on_resume: bool = False,
) -> CompactResumeResult:
    with acquire_workspace_lock(workspace_root):
        rel_path, target, before_content, frontmatter, body = _load_for_mutation(
            workspace_root, token
        )
        _ensure_not_archived(frontmatter)
        _verify_integrity(frontmatter, body)
        if frontmatter.handoff_prompt is None or not frontmatter.handoff_prompt.strip():
            raise ValidationError("Cannot resume a compact without a handoff_prompt")
        frontmatter.resume_count += 1
        frontmatter.state = "archived" if auto_archive_on_resume else "resumed"
        frontmatter.updated = _now()
        after_content = _render_compact(frontmatter, body)
        mutation = _record_compact_mutation(
            conn,
            workspace_root,
            target=target,
            operation_type="resume_compact",
            rel_path=rel_path,
            before_content=before_content,
            after_content=after_content,
            request_json={
                "token": frontmatter.id,
                "auto_archive_on_resume": auto_archive_on_resume,
            },
            token=frontmatter.id,
            state=frontmatter.state,
            document_sha256=frontmatter.document_sha256,
        )
        return CompactResumeResult(
            token=frontmatter.id,
            path=rel_path,
            operation_id=mutation.operation_id,
            snapshot_id=mutation.snapshot_id,
            state=frontmatter.state,
            resume_count=frontmatter.resume_count,
            handoff_prompt=frontmatter.handoff_prompt,
            body=body,
            document_sha256=frontmatter.document_sha256,
        )


def list_compacts(
    workspace_root: WorkspaceRoot,
    *,
    state: str | None = None,
    project: str | None = None,
    limit: int = 50,
) -> list[CompactListItem]:
    if state is not None and state not in ALLOWED_COMPACT_STATES:
        raise ValidationError(
            f"Invalid compact state {state!r}: must be one of {sorted(ALLOWED_COMPACT_STATES)}"
        )
    if limit < 1:
        raise ValidationError("limit must be at least 1")
    if limit > MAX_COMPACT_LIST_LIMIT:
        raise ValidationError(f"limit must not exceed {MAX_COMPACT_LIST_LIMIT}")
    rel_dir = contained_path(workspace_root, COMPACTS_DIR)
    if not rel_dir.exists():
        return []
    assert_no_symlink_escape(rel_dir, workspace_root)
    if not rel_dir.is_dir():
        return []

    items: list[CompactListItem] = []
    for entry in sorted(rel_dir.iterdir(), key=lambda path: path.name, reverse=True):
        match = COMPACT_FILENAME_RE.fullmatch(entry.name)
        if match is None or not entry.is_file():
            continue
        try:
            read = read_compact(workspace_root, token=match.group(1))
        except (CompactNotFoundError, FrontmatterInvalidError, ValidationError):
            continue
        fm = read.frontmatter
        if state is not None and fm.state != state:
            continue
        if project is not None and fm.project != project:
            continue
        items.append(
            CompactListItem(
                token=fm.id,
                path=read.path,
                project=fm.project,
                state=fm.state,
                resume_count=fm.resume_count,
                created=fm.created,
                updated=fm.updated,
                sources=fm.sources,
                tags=fm.tags,
                document_sha256=fm.document_sha256,
            )
        )
        if len(items) >= limit:
            break
    return items


def archive_compact(
    conn: DbConnection,
    workspace_root: WorkspaceRoot,
    *,
    token: str,
) -> CompactMutationResult:
    with acquire_workspace_lock(workspace_root):
        rel_path, target, before_content, frontmatter, body = _load_for_mutation(
            workspace_root, token
        )
        if frontmatter.state == "archived":
            raise CompactArchivedError("Compact is already archived", details={"token": token})
        frontmatter.state = "archived"
        frontmatter.updated = _now()
        after_content = _render_compact(frontmatter, body)
        return _record_compact_mutation(
            conn,
            workspace_root,
            target=target,
            operation_type="archive_compact",
            rel_path=rel_path,
            before_content=before_content,
            after_content=after_content,
            request_json={"token": frontmatter.id},
            token=frontmatter.id,
            state=frontmatter.state,
            document_sha256=frontmatter.document_sha256,
        )


def parse_compact(content: str) -> tuple[CompactFrontmatter, str]:
    fm_block, body = extract_frontmatter_block(content)
    if not fm_block:
        raise FrontmatterInvalidError("Compact frontmatter is required")
    raw_yaml = fm_block.removeprefix("---\n").removesuffix("---\n").removesuffix("---")
    try:
        loaded = safe_load_yaml(raw_yaml, max_bytes=MAX_COMPACT_BYTES)
    except yaml.YAMLError as exc:
        raise FrontmatterInvalidError("Compact frontmatter is invalid YAML") from exc
    if not isinstance(loaded, dict):
        raise FrontmatterInvalidError("Compact frontmatter must be a mapping")
    raw = cast(dict[object, object], loaded)
    try:
        frontmatter = CompactFrontmatter.model_validate(raw)
    except PydanticValidationError as exc:
        raise FrontmatterInvalidError("Compact frontmatter failed validation") from exc
    try:
        if validate_compact_token(frontmatter.id) != frontmatter.id:
            raise FrontmatterInvalidError("Compact id must be a normalized compact token")
        if frontmatter.project is not None:
            validate_project_key(frontmatter.project)
        if _dedupe(frontmatter.sources) != frontmatter.sources:
            raise FrontmatterInvalidError(
                "Compact sources must be unique, trimmed, non-empty strings"
            )
        if _dedupe(frontmatter.tags) != frontmatter.tags:
            raise FrontmatterInvalidError("Compact tags must be unique, trimmed, non-empty strings")
    except ValidationError as exc:
        raise FrontmatterInvalidError("Compact frontmatter values are invalid") from exc
    return frontmatter, body


def _record_compact_mutation(
    conn: DbConnection,
    workspace_root: WorkspaceRoot,
    *,
    target: Path,
    operation_type: str,
    rel_path: str,
    before_content: str | None,
    after_content: str,
    request_json: JsonObject,
    token: str,
    state: CompactState,
    document_sha256: str | None,
) -> CompactMutationResult:
    after_size = len(after_content.encode("utf-8"))
    if after_size > MAX_COMPACT_BYTES:
        raise FileTooLargeError(
            f"Compact mutation is {after_size} bytes; maximum is {MAX_COMPACT_BYTES}",
            details={
                "size_bytes": after_size,
                "max_bytes": MAX_COMPACT_BYTES,
                "scope": "compact",
            },
        )
    snapshot_id = new_snapshot_id()
    snapshot_dir = create_global_snapshot(
        workspace_root,
        snapshot_id=snapshot_id,
        operation_type=operation_type,
        target_project_key=None,
        reason=operation_type,
        before_files={} if before_content is None else {rel_path: before_content},
        after_files={rel_path: after_content},
    )
    atomic_write_text(target, after_content)
    record_snapshot_in_db(
        conn,
        snapshot_id=snapshot_id,
        project_key=WORKSPACE_OPERATION_PROJECT,
        target_path=rel_path,
        snapshot_dir=str(snapshot_dir),
        reason=operation_type,
    )
    op_id = record_operation(
        conn,
        project_key=WORKSPACE_OPERATION_PROJECT,
        operation_type=operation_type,
        tool_name=operation_type,
        target_path=rel_path,
        request_json=request_json,
        after_sha256=compute_sha256(after_content),
        snapshot_id=snapshot_id,
        state=OP_APPLIED,
    )
    return CompactMutationResult(
        token=token,
        path=rel_path,
        operation_id=op_id,
        snapshot_id=snapshot_id,
        state=state,
        document_sha256=document_sha256,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ensure_compacts_dir(workspace_root: WorkspaceRoot) -> Path:
    compacts_dir = contained_path(workspace_root, COMPACTS_DIR)
    if compacts_dir.exists():
        assert_no_symlink_escape(compacts_dir, workspace_root)
        if not compacts_dir.is_dir():
            raise ValidationError("workspace/compacts exists but is not a directory")
    else:
        compacts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    compacts_dir.chmod(0o700)
    return compacts_dir


def _compact_path(workspace_root: WorkspaceRoot, token: str) -> Path:
    rel_path = compact_relative_path(token)
    target = contained_path(workspace_root, rel_path)
    assert_no_symlink_escape(target.parent, workspace_root)
    if target.exists():
        assert_not_symlink(target)
    return target


def _existing_compact_path(workspace_root: WorkspaceRoot, token: str) -> tuple[str, Path]:
    normalized = validate_compact_token(token)
    rel_path = compact_relative_path(normalized)
    target = _compact_path(workspace_root, normalized)
    if not target.is_file():
        raise CompactNotFoundError("Compact not found", details={"token": normalized})
    return rel_path, target


def _load_for_mutation(
    workspace_root: WorkspaceRoot,
    token: str,
) -> tuple[str, Path, str, CompactFrontmatter, str]:
    rel_path, target = _existing_compact_path(workspace_root, token)
    if target.stat().st_size > MAX_COMPACT_BYTES:
        raise FileTooLargeError(
            f"Compact exceeds the {MAX_COMPACT_BYTES}-byte safety limit",
            details={"max_bytes": MAX_COMPACT_BYTES, "scope": "compact"},
        )
    before_content = target.read_text(encoding="utf-8")
    frontmatter, body = parse_compact(before_content)
    normalized = validate_compact_token(token)
    if frontmatter.id != normalized:
        raise FrontmatterInvalidError("Compact id does not match its filename token")
    return rel_path, target, before_content, frontmatter, body


def _unique_token(compacts_dir: Path) -> str:
    for _ in range(100):
        token = new_compact_token()
        if not (compacts_dir / f"compact_{token}.md").exists():
            return token
    raise ValidationError("Could not allocate a unique compact token")


def _render_compact(frontmatter: CompactFrontmatter, body: str) -> str:
    dumped = yaml.safe_dump(
        frontmatter.model_dump(mode="json"),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return f"---\n{dumped}---\n{body}"


def _dedupe(values: Sequence[str]) -> list[str]:
    if len(values) > MAX_COMPACT_ITEMS:
        raise ValidationError(f"Compact metadata accepts at most {MAX_COMPACT_ITEMS} items")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if len(item) > MAX_COMPACT_ITEM_CHARS:
            raise ValidationError(
                f"Compact metadata item exceeds {MAX_COMPACT_ITEM_CHARS} characters"
            )
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _ensure_not_archived(frontmatter: CompactFrontmatter) -> None:
    if frontmatter.state == "archived":
        raise CompactArchivedError("Compact is archived", details={"token": frontmatter.id})


def _verify_integrity(frontmatter: CompactFrontmatter, body: str) -> None:
    if frontmatter.document_sha256 is None:
        return
    current_sha = compute_sha256(body)
    if current_sha != frontmatter.document_sha256:
        raise CompactIntegrityError(
            "Compact body hash does not match document_sha256",
            details={
                "token": frontmatter.id,
                "expected_document_sha256": frontmatter.document_sha256,
                "actual_document_sha256": current_sha,
            },
        )


def _starts_with_handoff(body: str, handoff_prompt: str) -> bool:
    stripped = body.lstrip()
    prompt = handoff_prompt.strip()
    expected = f"## Handoff Prompt\n\n{prompt}"
    if not stripped.startswith(expected):
        return False
    remainder = stripped[len(expected) :]
    return not remainder or remainder.startswith("\n\n")
