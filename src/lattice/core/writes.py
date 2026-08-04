"""Central write service for all mutating operations.

All project-scoped writes go through this module, never through direct
filesystem access in MCP/CLI layers. Every mutation is snapshot-protected
and operation-logged; every proposal is bound to project + path + base
document hash with a 24 h TTL; hash guards make conflicting applies fail
closed.

Hard refusals (the closed list, 00 principle 1): archived targets
(``DOCUMENT_ARCHIVED``), protected frontmatter identity keys
(``FRONTMATTER_PROTECTED``), out-of-project paths (``WORKSPACE_MISMATCH``).
Everything else — edit policies, frozen structure — is echoed to the agent,
not blocked.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import difflib
import hashlib
import json
import logging
import mimetypes
import re
import shutil
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from lattice.core import uploads as upload_staging
from lattice.core.document_map import hash_line_range, split_document_lines
from lattice.core.documents import ParsedDocument, compute_sha256, parse_document_content
from lattice.core.edit_targets import ExactEdit, InsertAnchor
from lattice.core.errors import (
    CannotArchiveSpineError,
    ContentHashMismatchError,
    DocumentArchivedError,
    DocumentExistsError,
    DocumentNotFoundError,
    DownloadTimeoutError,
    FileTooLargeError,
    FrontmatterProtectedError,
    FrontmatterRequiredError,
    InvalidOperationError,
    LatticeError,
    OperationNotFoundError,
    PatchConflictError,
    PatchExpiredError,
    PatchProjectMismatchError,
    PathExistsError,
    SnapshotNotFoundError,
    TargetHashMismatchError,
    UnknownFolderError,
    UnsupportedFileTypeError,
    UploadIncompleteError,
    ValidationError,
    WorkspaceMismatchError,
)
from lattice.core.file_io import atomic_write_bytes, atomic_write_text
from lattice.core.folders import (
    CREATABLE_FOLDERS,
    PROJECT_DIRECTORIES,
    SPINE_FILENAME,
    archive_path_for,
    folder_of,
    is_archived_path,
    origin_path_for,
)
from lattice.core.frontmatter import (
    REQUIRED_FRONTMATTER_KEYS,
    extract_frontmatter_block,
    generate_frontmatter,
    is_managed_markdown,
    new_document_id,
    parse_frontmatter,
    set_frontmatter_updated,
)
from lattice.core.images import ImagePolicy, compress_image_for_storage
from lattice.core.indexer import index_file, remove_from_index
from lattice.core.locks import acquire_project_lock, acquire_workspace_lock
from lattice.core.operations import (
    OP_APPLIED,
    OP_DISCARDED,
    OP_EXPIRED,
    OP_FAILED,
    OP_PENDING,
    OP_STALE,
    PROPOSAL_OP_TYPES,
    OperationRecord,
    find_equivalent_pending_proposal,
    get_operation,
    is_expired,
    mark_operation_state,
    new_proposal_id,
    record_operation,
    record_proposal,
    sweep_expired_proposals,
)
from lattice.core.patches import (
    PreparedPatch,
    prepare_exact_replace_patch,
    prepare_frontmatter_patch,
    prepare_insert_patch,
    prepare_multi_edit_patch,
    prepare_range_patch,
    prepare_search_replace_patch,
    prepare_section_patch,
)
from lattice.core.paths import (
    PathSafetyError,
    WorkspaceRoot,
    contained_path,
    contained_project_root,
    is_under_root,
)
from lattice.core.policy import PolicyEcho, policy_echo_for
from lattice.core.reconcile import reconcile_document
from lattice.core.registry import (
    ProjectEntry,
    load_registry,
    save_registry,
    serialize_registry,
    validate_project_key,
)
from lattice.core.remote_fetch import DEFAULT_TOTAL_TIMEOUT, fetch_remote_file
from lattice.core.snapshots import (
    create_global_snapshot,
    create_snapshot,
    create_upload_snapshot,
    find_snapshot_dir,
    new_snapshot_id,
    read_snapshot_before_content,
    read_snapshot_metadata,
    record_snapshot_in_db,
)
from lattice.core.types import DbConnection, JsonObject

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".md"}

#: Extensions refused for upload_library_file (scripts/executables); every
#: other extension, including none, is allowed. Deliberately permissive by
#: design (denylist, not allowlist) per the upload feature's own decision.
BLOCKED_UPLOAD_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".psm1",
        ".psd1",
        ".bat",
        ".cmd",
        ".com",
        ".exe",
        ".msi",
        ".dll",
        ".scr",
        ".py",
        ".pyc",
        ".pyo",
        ".pyw",
        ".md",
        ".rb",
        ".pl",
        ".php",
        ".php3",
        ".php4",
        ".php5",
        ".js",
        ".mjs",
        ".cjs",
        ".vbs",
        ".vbe",
        ".wsf",
        ".wsh",
        ".jar",
        ".war",
        ".apk",
        ".app",
        ".command",
        ".action",
        ".workflow",
        ".dylib",
        ".so",
        ".deb",
        ".rpm",
        ".dmg",
        ".gadget",
        ".hta",
        ".reg",
        ".lnk",
    }
)

#: Cap on a single tool call's decoded payload: upload_library_file's whole
#: content_base64, or one append_upload_chunk chunk. Kept small deliberately
#: — real MCP-client tool-call size ceilings (ChatGPT's connector included)
#: turned out to be far below what the wire format alone would allow, so a
#: call this size actually has to be deliverable, not just "under a cap we
#: picked." The base64 wire payload runs ~33% larger than this.
MAX_CHUNK_BYTES: Final = 256 * 1024

#: Cap on the *assembled* file size for a chunked upload (start/finalize's
#: total_size) — not a per-call limit, since finalize_library_file_upload
#: builds this from many MAX_CHUNK_BYTES-sized pieces. upload_library_file
#: (single call, no chunking) is capped at MAX_CHUNK_BYTES instead, since
#: its whole payload has to fit in one call.
MAX_UPLOAD_BYTES: Final = 20 * 1024 * 1024

# A caller controls ``total_chunks`` and finalize walks ``range(total_chunks)``.
# Keep that work independently bounded even when the declared byte size is
# tiny. At the 256 KiB hint, a maximum-size upload needs only 80 chunks.
MAX_UPLOAD_CHUNKS: Final = 1024
MAX_PENDING_UPLOAD_SESSIONS_PER_PROJECT: Final = 32
MAX_PENDING_UPLOAD_BYTES_PER_PROJECT: Final = 256 * 1024 * 1024
MAX_MUTATED_MARKDOWN_BYTES: Final = 5 * 1024 * 1024
MAX_UPLOAD_METADATA_BYTES: Final = 64 * 1024
MAX_UPLOAD_FILENAME_BYTES: Final = 255
MAX_TITLE_CHARS: Final = 512
MAX_MIME_TYPE_CHARS: Final = 255
_SHA256_RE: Final = re.compile(r"^[0-9a-fA-F]{64}$")

PatchMode = Literal["body", "full"]


class WriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    snapshot_id: str | None = None
    restored_from_snapshot_id: str | None = None
    rollback_snapshot_id: str | None = None
    path: str
    folder: str | None = None
    document_sha256: str | None = None
    diff: str = ""
    index_error: str | None = None


class ProposalResult(BaseModel):
    """Result of a ``propose_*`` call: a pending edit, not a saved one."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    project_key: str
    path: str
    folder: str
    proposal_kind: str
    document_before_sha256: str | None = None
    target_before_sha256: str | None = None
    after_sha256: str
    diff: str
    expires_at: str
    policy: PolicyEcho
    deduped: bool = False


def _validate_path_safe(
    workspace_root: Path,
    project_key: str,
    user_path: str,
    *,
    extensions_denylist: frozenset[str] | None = None,
) -> Path:
    """Validate a project-relative path is safe and within the project.

    By default, only ``ALLOWED_EXTENSIONS`` (Markdown) may be targeted. Pass
    ``extensions_denylist`` to invert that check instead — block only the
    given extensions and allow everything else (used by uploads, which are
    not Markdown).
    """
    project_root = contained_project_root(workspace_root, project_key)
    try:
        resolved = contained_path(project_root, user_path)
    except PathSafetyError as exc:
        raise WorkspaceMismatchError(
            f"Path {user_path!r} is outside the asserted project boundary"
        ) from exc
    if not is_under_root(resolved, project_root):
        msg = f"Path {user_path!r} escapes the project root"
        raise WorkspaceMismatchError(msg)
    suffix = resolved.suffix.lower()
    if any(part.startswith(".") for part in Path(user_path).parts):
        raise WorkspaceMismatchError("Hidden paths and .lattice internals cannot be targeted")
    if extensions_denylist is not None:
        if suffix in extensions_denylist:
            msg = f"Uploads of {suffix!r} files are not allowed"
            raise UnsupportedFileTypeError(msg, details={"extension": suffix})
    elif suffix not in ALLOWED_EXTENSIONS:
        msg = f"Unsupported file extension {suffix}: only {sorted(ALLOWED_EXTENSIONS)} allowed"
        raise ValidationError(msg)
    return resolved


def _validate_markdown_size(content: str) -> None:
    size = len(content.encode("utf-8"))
    if size > MAX_MUTATED_MARKDOWN_BYTES:
        raise FileTooLargeError(
            f"Markdown mutation is {size} bytes; maximum is {MAX_MUTATED_MARKDOWN_BYTES}",
            details={
                "size_bytes": size,
                "max_bytes": MAX_MUTATED_MARKDOWN_BYTES,
                "scope": "document",
            },
        )


def _validated_metadata(metadata: JsonObject | None) -> JsonObject:
    value = dict(metadata or {})
    encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_UPLOAD_METADATA_BYTES:
        raise FileTooLargeError(
            f"Upload metadata exceeds the {MAX_UPLOAD_METADATA_BYTES}-byte limit",
            details={"max_bytes": MAX_UPLOAD_METADATA_BYTES, "scope": "metadata"},
        )
    return value


def _validated_mime_type(mime_type: str | None) -> str | None:
    if mime_type is None:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in mime_type):
        raise ValidationError("mime_type must not contain control characters")
    value = mime_type.strip()
    if not value:
        return None
    if len(value) > MAX_MIME_TYPE_CHARS or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValidationError(
            f"mime_type must be at most {MAX_MIME_TYPE_CHARS} characters "
            "and contain no control characters"
        )
    return value


def _generate_diff_text(before: str, after: str, path: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


def _slugify_title(title: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return normalized[:60] or "document"


def _read_document_for_edit(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    path: str,
) -> tuple[Path, ParsedDocument]:
    """Resolve an editable project document, reconciling out-of-band drift first.

    Enforces the hard refusals: missing file → ``DOCUMENT_NOT_FOUND``;
    archived target (status or ``archive/`` path) → ``DOCUMENT_ARCHIVED``.
    """
    target_file = _validate_path_safe(workspace_root, project_key, path)
    reconcile_document(conn, workspace_root, project_key, path)
    if not target_file.is_file():
        raise DocumentNotFoundError(f"Document not found: {path}")
    content = target_file.read_text(encoding="utf-8")
    parsed = parse_document_content(content, project_key=project_key, path=path)
    if is_archived_path(path) or parsed.status == "archived":
        raise DocumentArchivedError(
            f"{path} is archived; unarchive it before editing",
            details={"path": path, "status": parsed.status},
        )
    return target_file, parsed


def _check_expected_document_hash(current: str, expected: str | None) -> None:
    if expected is not None and current != expected:
        msg = "Document changed since it was inspected (expected_document_sha256 mismatch)"
        raise PatchConflictError(
            msg,
            details={
                "expected_document_sha256": expected,
                "current_document_sha256": current,
                "recommended_action": (
                    "Retry with current_document_sha256 as expected_document_sha256 if the "
                    "target text is unchanged; otherwise re-read the document first."
                ),
            },
        )


def _check_expected_target_hash(
    actual: str | None,
    expected: str | None,
    *,
    current_target_content: str | None = None,
) -> None:
    if expected is not None and actual is not None and actual != expected:
        msg = "Target changed since it was inspected (expected target hash mismatch)"
        details: JsonObject = {
            "expected_target_sha256": expected,
            "current_target_sha256": actual,
            "recommended_action": (
                "The target content differs from what was inspected. Use the current "
                "target content below (or re-read the range) and propose again with "
                "current_target_sha256 as the expected target hash."
            ),
        }
        if current_target_content is not None:
            details["current_target_content"] = current_target_content
        raise TargetHashMismatchError(msg, details=details)


def _record_proposal_result(
    conn: DbConnection,
    *,
    parsed: ParsedDocument,
    operation_type: str,
    prepared: PreparedPatch,
) -> ProposalResult:
    """Record a proposal (no file mutation) and build its result."""
    # Validate the complete result, including managed identity and behavioral
    # frontmatter, before persisting a proposal that could later be applied.
    parse_document_content(
        prepared.new_full_content,
        project_key=parsed.project_key,
        path=parsed.path,
    )
    _validate_markdown_size(prepared.new_full_content)
    after_sha256 = compute_sha256(prepared.new_full_content)
    diff = _generate_diff_text(parsed.content, prepared.new_full_content, parsed.path)
    request_json: JsonObject = {
        "path": parsed.path,
        "proposal_kind": prepared.proposal_kind,
        "target": prepared.target,
        "new_content": prepared.new_full_content,
        "document_before_sha256": parsed.sha256,
        "target_before_sha256": prepared.target_before_sha256,
        "after_sha256": after_sha256,
    }
    existing = find_equivalent_pending_proposal(
        conn,
        project_key=parsed.project_key,
        operation_type=operation_type,
        target_path=parsed.path,
        base_sha256=parsed.sha256,
        target=prepared.target,
    )
    if existing is not None and not is_expired(existing):
        op_id = existing.id
        expires_at = existing.expires_at or ""
        deduped = True
    else:
        op_id, expires_at = record_proposal(
            conn,
            project_key=parsed.project_key,
            operation_type=operation_type,
            target_path=parsed.path,
            request_json=request_json,
            base_sha256=parsed.sha256,
            after_sha256=after_sha256,
            diff_text=diff,
        )
        deduped = False
    return ProposalResult(
        operation_id=op_id,
        project_key=parsed.project_key,
        path=parsed.path,
        folder=parsed.folder,
        proposal_kind=prepared.proposal_kind,
        document_before_sha256=parsed.sha256,
        target_before_sha256=prepared.target_before_sha256,
        after_sha256=after_sha256,
        diff=diff,
        expires_at=expires_at,
        policy=policy_echo_for(parsed),
        deduped=deduped,
    )


# ── Proposal tools ───────────────────────────────────────────────────────────


def propose_section_patch(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    path: str,
    section_id: str,
    expected_document_sha256: str,
    expected_section_sha256: str,
    new_content: str,
) -> ProposalResult:
    """Propose replacing a single heading-derived section."""
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        _file, parsed = _read_document_for_edit(conn, workspace_root, project_key, path)
        _check_expected_document_hash(parsed.sha256, expected_document_sha256)
        prepared = prepare_section_patch(parsed.content, section_id, new_content)
        section_start = prepared.target.get("start_line")
        section_end = prepared.target.get("end_line")
        section_content: str | None = None
        if isinstance(section_start, int) and isinstance(section_end, int):
            current_lines = split_document_lines(parsed.content)
            section_content = "\n".join(current_lines[section_start - 1 : section_end])
        _check_expected_target_hash(
            prepared.target_before_sha256,
            expected_section_sha256,
            current_target_content=section_content,
        )
        return _record_proposal_result(
            conn, parsed=parsed, operation_type="propose_section_patch", prepared=prepared
        )


def propose_range_patch(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    path: str,
    start_line: int,
    end_line: int,
    expected_document_sha256: str,
    expected_range_sha256: str,
    new_content: str,
) -> ProposalResult:
    """Propose replacing a specific line range (covers single-line edits)."""
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        _file, parsed = _read_document_for_edit(conn, workspace_root, project_key, path)
        _check_expected_document_hash(parsed.sha256, expected_document_sha256)
        prepared = prepare_range_patch(parsed.content, start_line, end_line, new_content)
        current_lines = split_document_lines(parsed.content)
        _check_expected_target_hash(
            prepared.target_before_sha256,
            expected_range_sha256,
            current_target_content="\n".join(current_lines[start_line - 1 : end_line]),
        )
        return _record_proposal_result(
            conn, parsed=parsed, operation_type="propose_range_patch", prepared=prepared
        )


def propose_search_replace_patch(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    path: str,
    find: str,
    replace: str,
    mode: Literal["literal", "regex"] = "literal",
    case_sensitive: bool = False,
    occurrence: int | Literal["all"] = 1,
    expected_document_sha256: str,
    expected_match_count: int | None = None,
    include_code_blocks: bool = True,
) -> ProposalResult:
    """Propose replacing a specific match or a controlled set of matches."""
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        _file, parsed = _read_document_for_edit(conn, workspace_root, project_key, path)
        _check_expected_document_hash(parsed.sha256, expected_document_sha256)
        prepared = prepare_search_replace_patch(
            parsed.content,
            find=find,
            replace=replace,
            mode=mode,
            case_sensitive=case_sensitive,
            occurrence=occurrence,
            expected_match_count=expected_match_count,
            include_code_blocks=include_code_blocks,
        )
        return _record_proposal_result(
            conn, parsed=parsed, operation_type="propose_search_replace_patch", prepared=prepared
        )


def propose_insert_patch(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    path: str,
    anchor: InsertAnchor,
    content: str,
    expected_document_sha256: str,
) -> ProposalResult:
    """Propose inserting content before/after a safe anchor."""
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        _file, parsed = _read_document_for_edit(conn, workspace_root, project_key, path)
        _check_expected_document_hash(parsed.sha256, expected_document_sha256)
        prepared = prepare_insert_patch(parsed.content, anchor, content)
        return _record_proposal_result(
            conn, parsed=parsed, operation_type="propose_insert_patch", prepared=prepared
        )


def propose_exact_replace_patch(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    path: str,
    old_string: str,
    new_string: str,
    occurrence: int | Literal["all"] | None = None,
    expected_match_count: int | None = None,
    expected_document_sha256: str | None = None,
) -> ProposalResult:
    """Propose replacing an exact (possibly multi-line) occurrence of text.

    The matched text is the guard, so ``expected_document_sha256`` is
    optional extra safety rather than a requirement.
    """
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        _file, parsed = _read_document_for_edit(conn, workspace_root, project_key, path)
        _check_expected_document_hash(parsed.sha256, expected_document_sha256)
        prepared = prepare_exact_replace_patch(
            parsed.content,
            old_string=old_string,
            new_string=new_string,
            occurrence=occurrence,
            expected_match_count=expected_match_count,
        )
        return _record_proposal_result(
            conn, parsed=parsed, operation_type="propose_exact_replace_patch", prepared=prepared
        )


def propose_multi_edit_patch(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    path: str,
    edits: list[ExactEdit],
    expected_document_sha256: str | None = None,
) -> ProposalResult:
    """Propose an atomic batch of exact-replace edits as a single pending patch."""
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        _file, parsed = _read_document_for_edit(conn, workspace_root, project_key, path)
        _check_expected_document_hash(parsed.sha256, expected_document_sha256)
        prepared = prepare_multi_edit_patch(parsed.content, edits=edits)
        return _record_proposal_result(
            conn, parsed=parsed, operation_type="propose_multi_edit_patch", prepared=prepared
        )


def propose_frontmatter_patch(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    path: str,
    set_values: JsonObject,
    remove_keys: list[str],
    expected_document_sha256: str | None = None,
) -> ProposalResult:
    """Propose setting/removing individual frontmatter keys (identity keys protected)."""
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        _file, parsed = _read_document_for_edit(conn, workspace_root, project_key, path)
        _check_expected_document_hash(parsed.sha256, expected_document_sha256)
        prepared = prepare_frontmatter_patch(
            parsed.content, set_values=set_values, remove_keys=remove_keys
        )
        return _record_proposal_result(
            conn, parsed=parsed, operation_type="propose_frontmatter_patch", prepared=prepared
        )


def propose_patch(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    path: str,
    new_content: str,
    mode: PatchMode = "body",
    expected_document_sha256: str | None = None,
) -> ProposalResult:
    """Propose a coarse body/full replacement (the fallback patch tool).

    ``mode=body`` replaces only the Markdown body, preserving managed
    frontmatter; ``mode=full`` replaces the whole file and must keep valid,
    matching required frontmatter on managed documents.
    """
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        _file, parsed = _read_document_for_edit(conn, workspace_root, project_key, path)
        _check_expected_document_hash(parsed.sha256, expected_document_sha256)
        resolved_content = _resolve_patch_content(
            mode=mode,
            project_key=project_key,
            current_content=parsed.content,
            new_content=new_content,
        )
        prepared = PreparedPatch(
            proposal_kind=mode,
            new_full_content=resolved_content,
            target_before_sha256=None,
            target={
                "kind": mode,
                "new_content_sha256": compute_sha256(new_content),
            },
        )
        return _record_proposal_result(
            conn, parsed=parsed, operation_type="propose_patch", prepared=prepared
        )


class DiscardPatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    path: str | None = None
    state: str = OP_DISCARDED


def discard_patch(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    operation_id: str,
) -> DiscardPatchResult:
    """Discard a pending patch proposal so it can no longer be applied.

    Only operation-log metadata changes; user Markdown is untouched.
    """
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        op = _get_proposal_for_action(conn, project_key, operation_id)
        if op.state != OP_PENDING:
            msg = f"Operation {operation_id} has state {op.state!r}, expected pending"
            raise InvalidOperationError(msg, details={"state": op.state})
        mark_operation_state(conn, operation_id, OP_DISCARDED)
        return DiscardPatchResult(operation_id=operation_id, path=op.target_path)


def _get_proposal_for_action(
    conn: DbConnection, project_key: str, operation_id: str
) -> OperationRecord:
    op = get_operation(conn, operation_id)
    if op is None:
        raise OperationNotFoundError(f"Operation {operation_id} not found")
    if op.operation_type not in PROPOSAL_OP_TYPES:
        raise InvalidOperationError(f"Operation {operation_id} is not a patch proposal")
    if op.project_key != project_key:
        msg = (
            f"Operation {operation_id} belongs to project {op.project_key!r}, "
            f"not the asserted project {project_key!r}"
        )
        raise PatchProjectMismatchError(msg)
    return op


# ── Apply ────────────────────────────────────────────────────────────────────


def apply_patch(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    operation_id: str,
) -> WriteResult:
    """Apply a previously proposed patch.

    Revalidates the proposal binding (project + path + base hash) and its
    TTL, reconciles out-of-band drift, snapshots before writing, and returns
    the new ``document_sha256`` for hash chaining.
    """
    project_dir = contained_project_root(workspace_root, project_key)

    with acquire_project_lock(project_dir, project_key):
        op = _get_proposal_for_action(conn, project_key, operation_id)

        if op.state == OP_PENDING and is_expired(op):
            mark_operation_state(conn, operation_id, OP_EXPIRED)
            op = op.model_copy(update={"state": OP_EXPIRED})

        if op.state == OP_EXPIRED:
            raise PatchExpiredError(
                f"Proposal {operation_id} expired; proposals are valid for 24 h. "
                "Re-read the document and propose again.",
                details={"expires_at": op.expires_at},
            )
        if op.state == OP_STALE:
            raise PatchConflictError(
                f"Proposal {operation_id} was invalidated by an out-of-band edit of "
                f"{op.target_path}; re-read and re-propose.",
                details={"reason": "out-of-band-edit", "path": op.target_path},
            )
        if op.state != OP_PENDING:
            msg = f"Operation {operation_id} has state {op.state!r}, expected pending"
            raise InvalidOperationError(msg, details={"state": op.state})

        target_path_str = op.target_path or ""
        target_file = _validate_path_safe(workspace_root, project_key, target_path_str)
        if not is_under_root(target_file, project_dir):
            msg = f"Operation {operation_id} target is outside the project {project_key!r}"
            raise WorkspaceMismatchError(msg)

        # Reconcile before the hash check so an out-of-band edit both stales
        # the proposal and produces the right conflict below.
        outcome = reconcile_document(conn, workspace_root, project_key, target_path_str)

        new_content = op.request_json.get("new_content")
        if not isinstance(new_content, str):
            raise InvalidOperationError(f"Operation {operation_id} has no new_content in request")
        _validate_markdown_size(new_content)
        prepared_sha256 = compute_sha256(new_content)
        if op.after_sha256 is None or prepared_sha256 != op.after_sha256:
            raise InvalidOperationError(
                f"Operation {operation_id} prepared content failed its integrity check"
            )
        # Pending operations are durable, untrusted input at apply time. Parse
        # them again before any snapshot or filesystem mutation so corruption
        # cannot bypass path-role, frontmatter, status, or policy validation.
        parse_document_content(
            new_content,
            project_key=project_key,
            path=target_path_str,
        )

        current_sha256: str | None = None
        current_content = ""
        if target_file.is_file():
            current_content = target_file.read_text(encoding="utf-8")
            current_sha256 = compute_sha256(current_content)

        if current_sha256 != op.base_sha256:
            record_operation(
                conn,
                project_key=project_key,
                operation_type="apply_patch",
                tool_name="apply_patch",
                target_path=target_path_str,
                request_json={"operation_id": operation_id},
                base_sha256=current_sha256,
                after_sha256=op.after_sha256,
                state=OP_FAILED,
            )
            details: JsonObject = {
                "proposal_document_sha256": op.base_sha256,
                "current_document_sha256": current_sha256,
                "recommended_action": (
                    "The document changed after this patch was proposed. Re-read the "
                    "current content and create a fresh proposal."
                ),
            }
            if outcome.drifted:
                details["reason"] = "out-of-band-edit"
            msg = f"Patch conflict: {target_path_str} has changed since the patch was proposed"
            raise PatchConflictError(msg, details=details)

        _verify_target_hash_for_apply(op.request_json, current_content)

        diff_text = op.diff_text or ""
        snapshot_id = new_snapshot_id()
        snapshot_dir = create_snapshot(
            project_dir,
            project_key=project_key,
            target_path=target_path_str,
            before_content=current_content,
            after_content=new_content,
            reason="apply_patch",
            snapshot_id=snapshot_id,
        )

        after_sha256 = prepared_sha256
        try:
            atomic_write_text(target_file, new_content)
            record_snapshot_in_db(
                conn,
                snapshot_id=snapshot_id,
                project_key=project_key,
                target_path=target_path_str,
                snapshot_dir=str(snapshot_dir),
                reason="apply_patch",
                commit=False,
            )

            new_op_id = record_operation(
                conn,
                project_key=project_key,
                operation_type="apply_patch",
                tool_name="apply_patch",
                target_path=target_path_str,
                request_json={"operation_id": operation_id},
                base_sha256=current_sha256,
                after_sha256=after_sha256,
                diff_text=diff_text,
                snapshot_id=snapshot_id,
                state=OP_APPLIED,
                commit=False,
            )

            # The proposal and its durable apply record become terminal in
            # the same SQLite commit. A second apply therefore cannot observe
            # a half-published bookkeeping state.
            mark_operation_state(
                conn,
                operation_id,
                OP_APPLIED,
                commit=False,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            try:
                atomic_write_text(target_file, current_content)
            except OSError as rollback_error:
                logger.critical(
                    "Patch rollback could not restore prior content (type=%s)",
                    type(rollback_error).__name__,
                )
            try:
                shutil.rmtree(snapshot_dir)
            except OSError as cleanup_error:
                logger.error(
                    "Patch rollback could not remove its snapshot (type=%s)",
                    type(cleanup_error).__name__,
                )
            raise

        index_error = _reindex_after_write(conn, workspace_root, project_key, target_file)

    return WriteResult(
        operation_id=new_op_id,
        snapshot_id=snapshot_id,
        path=target_path_str,
        folder=folder_of(target_path_str),
        document_sha256=after_sha256,
        diff=diff_text,
        index_error=index_error,
    )


def _verify_target_hash_for_apply(request_json: JsonObject, current_content: str) -> None:
    """Re-verify the granular target hash at apply time (defense in depth).

    The document-hash check already guarantees the whole file is unchanged,
    but for range and section proposals we additionally recompute the target
    hash so a corrupted/edited operation record can never apply a stale
    target.
    """
    target = request_json.get("target")
    expected = request_json.get("target_before_sha256")
    if not isinstance(target, dict) or not isinstance(expected, str):
        return
    kind = target.get("kind")
    if kind not in ("range", "section"):
        return
    start_line = target.get("start_line")
    end_line = target.get("end_line")
    if not isinstance(start_line, int) or not isinstance(end_line, int):
        return
    lines = split_document_lines(current_content)
    if start_line < 1 or end_line > len(lines) or end_line < start_line:
        raise TargetHashMismatchError("Stored target range no longer fits the current document")
    actual = hash_line_range(lines, start_line, end_line)
    if actual != expected:
        raise TargetHashMismatchError("Target changed since the patch was proposed")


def _reindex_after_write(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    target_file: Path,
) -> str | None:
    """Re-index a file after a write, recording a structured failure.

    Returns ``None`` on success or an error message string when re-indexing
    failed. A failed re-index is recorded as a failed operation so the audit
    log reflects the inconsistent index state.
    """
    project_dir = contained_project_root(workspace_root, project_key)
    rel = target_file.relative_to(project_dir).as_posix()
    try:
        index_file(conn, workspace_root, project_key, target_file)
    except (OSError, ValueError, sqlite3.Error) as exc:
        safe_error = f"Index refresh failed ({type(exc).__name__})"
        try:
            record_operation(
                conn,
                project_key=project_key,
                operation_type="reindex",
                target_path=rel,
                request_json={"error_type": type(exc).__name__},
                state=OP_FAILED,
            )
        except sqlite3.Error as audit_error:
            conn.rollback()
            logger.error(
                "Failed to record a derived-index error (type=%s)",
                type(audit_error).__name__,
            )
        return safe_error
    return None


def _remove_from_index_after_write(
    conn: DbConnection,
    project_key: str,
    path: str,
) -> str | None:
    """Remove a stale derived-index row without falsifying mutation failure."""
    try:
        remove_from_index(conn, project_key, path)
    except (OSError, ValueError, sqlite3.Error) as exc:
        conn.rollback()
        safe_error = f"Index removal failed ({type(exc).__name__})"
        try:
            record_operation(
                conn,
                project_key=project_key,
                operation_type="reindex",
                target_path=path,
                request_json={"error_type": type(exc).__name__},
                state=OP_FAILED,
            )
        except sqlite3.Error as audit_error:
            conn.rollback()
            logger.error(
                "Failed to record a derived-index removal error (type=%s)",
                type(audit_error).__name__,
            )
        return safe_error
    return None


def _combine_index_errors(*errors: str | None) -> str | None:
    present = [error for error in errors if error is not None]
    return "; ".join(present) if present else None


# ── Direct writes ────────────────────────────────────────────────────────────


def _resolve_patch_content(
    *,
    mode: PatchMode,
    project_key: str,
    current_content: str,
    new_content: str,
) -> str:
    """Resolve the final file content for a coarse patch, protecting frontmatter."""
    if mode == "body":
        fm_block, _body = extract_frontmatter_block(current_content)
        if not fm_block:
            return new_content
        now = datetime.now(UTC).isoformat()
        return set_frontmatter_updated(fm_block, now) + new_content
    if current_content and is_managed_markdown(current_content):
        existing_fm = parse_frontmatter(current_content)
        new_fm = parse_frontmatter(new_content)
        _validate_full_mode_frontmatter(
            existing_fm,
            new_fm,
            project_key,
            existing_content=current_content,
            new_content=new_content,
        )
        new_fm_block, new_body = extract_frontmatter_block(new_content)
        now = datetime.now(UTC).isoformat()
        return set_frontmatter_updated(new_fm_block, now) + new_body
    return new_content


def _validate_full_mode_frontmatter(
    existing_fm: JsonObject,
    new_fm: JsonObject,
    project_key: str,
    *,
    existing_content: str,
    new_content: str,
) -> None:
    """Ensure full-mode replacement keeps valid, compatible required frontmatter."""
    for key in REQUIRED_FRONTMATTER_KEYS:
        value = new_fm.get(key)
        if not isinstance(value, str) or not value.strip():
            msg = f"Patched content is missing required frontmatter field '{key}'"
            raise FrontmatterRequiredError(msg)

    for key in REQUIRED_FRONTMATTER_KEYS:
        existing_value = existing_fm.get(key)
        if isinstance(existing_value, str) and existing_value != str(new_fm[key]):
            msg = (
                f"Patched frontmatter '{key}' ({new_fm[key]!r}) does not match "
                f"the existing managed value ({existing_value!r})"
            )
            raise FrontmatterRequiredError(msg)

    for key in ("created", "updated"):
        existing_value = existing_fm.get(key)
        new_value = new_fm.get(key)
        if not isinstance(existing_value, str) or not existing_value.strip():
            raise FrontmatterRequiredError(
                f"Existing managed content is missing required frontmatter field '{key}'"
            )
        if not isinstance(new_value, str) or not new_value.strip():
            raise FrontmatterRequiredError(
                f"Patched content is missing required frontmatter field '{key}'"
            )
        if new_value != existing_value:
            raise FrontmatterProtectedError(
                f"Frontmatter key '{key}' is managed by Lattice and cannot be edited",
                details={"protected_keys": [key]},
            )
        existing_line = _frontmatter_field_line(existing_content, key)
        new_line = _frontmatter_field_line(new_content, key)
        if existing_line is None or new_line is None:
            raise FrontmatterRequiredError(
                f"Managed frontmatter field '{key}' must use a single scalar line"
            )
        if new_line != existing_line:
            raise FrontmatterProtectedError(
                f"Frontmatter key '{key}' is managed by Lattice and cannot be edited",
                details={"protected_keys": [key]},
            )

    if str(new_fm["project"]) != project_key:
        msg = (
            f"Patched frontmatter project '{new_fm['project']}' does not match "
            f"the asserted project '{project_key}'"
        )
        raise FrontmatterRequiredError(msg)


def _frontmatter_field_line(content: str, key: str) -> str | None:
    fm_block, _body = extract_frontmatter_block(content)
    match = re.search(rf"^{re.escape(key)}[ \t]*:[^\r\n]*$", fm_block, re.MULTILINE)
    return match.group(0) if match is not None else None


def _write_new_document(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    doc_rel: str,
    full_content: str,
    operation_type: str,
    request_json: JsonObject,
) -> WriteResult:
    """Shared machinery for snapshot-protected new-file writes."""
    _validate_markdown_size(full_content)
    project_dir = contained_project_root(workspace_root, project_key)
    target_file = _validate_path_safe(workspace_root, project_key, doc_rel)

    with acquire_project_lock(project_dir, project_key):
        if target_file.exists():
            raise DocumentExistsError(
                f"Document already exists: {doc_rel}",
                details={
                    "path": doc_rel,
                    "recommended_action": (
                        "Choose a different title/filename, or edit the existing document "
                        "with the propose/apply tools."
                    ),
                },
            )
        sha256 = compute_sha256(full_content)
        snapshot_id = new_snapshot_id()
        snapshot_dir = create_snapshot(
            project_dir,
            project_key=project_key,
            target_path=doc_rel,
            before_content=None,
            after_content=full_content,
            reason=operation_type,
            snapshot_id=snapshot_id,
        )
        try:
            atomic_write_text(target_file, full_content)
            record_snapshot_in_db(
                conn,
                snapshot_id=snapshot_id,
                project_key=project_key,
                target_path=doc_rel,
                snapshot_dir=str(snapshot_dir),
                reason=operation_type,
                commit=False,
            )
            op_id = record_operation(
                conn,
                project_key=project_key,
                operation_type=operation_type,
                tool_name=operation_type,
                target_path=doc_rel,
                request_json=request_json,
                after_sha256=sha256,
                snapshot_id=snapshot_id,
                state=OP_APPLIED,
                commit=False,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            target_file.unlink(missing_ok=True)
            try:
                shutil.rmtree(snapshot_dir)
            except OSError as cleanup_error:
                logger.error(
                    "New-document rollback could not remove its snapshot (type=%s)",
                    type(cleanup_error).__name__,
                )
            raise
        index_error = _reindex_after_write(conn, workspace_root, project_key, target_file)

    return WriteResult(
        operation_id=op_id,
        snapshot_id=snapshot_id,
        path=doc_rel,
        folder=folder_of(doc_rel),
        document_sha256=sha256,
        index_error=index_error,
    )


def create_document(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    folder_path: str,
    title: str,
    content: str,
    status: str | None = None,
    edit_policy: str | None = None,
) -> WriteResult:
    """Create a new managed Markdown document in a role folder.

    ``folder_path`` must start with a creatable role folder (rules, canvases,
    memory, library, inbox) and may nest freely below it; ``UNKNOWN_FOLDER``
    otherwise. The filename is derived from the title.
    """
    _validate_title(title)
    folder_path = _canonical_folder_path(folder_path, allow_empty=False)
    if not folder_path:
        raise UnknownFolderError(
            "folder_path must start with a role folder",
            details={"allowed_folders": list(CREATABLE_FOLDERS)},
        )
    first_segment = folder_path.split("/", 1)[0]
    if first_segment not in CREATABLE_FOLDERS:
        msg = (
            f"folder_path {folder_path!r} does not start with a creatable role folder; "
            f"allowed: {list(CREATABLE_FOLDERS)}"
        )
        raise UnknownFolderError(msg, details={"allowed_folders": list(CREATABLE_FOLDERS)})

    doc_rel = f"{folder_path}/{_slugify_title(title)}.md"
    fm = generate_frontmatter(
        doc_id=new_document_id(),
        project_key=project_key,
        title=title,
        status=status or "active",
        edit_policy=edit_policy,
    )
    body = content if content.endswith("\n") else content + "\n"
    request_json: JsonObject = {
        "folder_path": folder_path,
        "title": title,
        "status": status,
        "edit_policy": edit_policy,
    }
    return _write_new_document(
        conn,
        workspace_root,
        project_key,
        doc_rel=doc_rel,
        full_content=fm + body,
        operation_type="create_document",
        request_json=request_json,
    )


def capture_note(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    text: str,
    title: str | None = None,
) -> WriteResult:
    """Capture a note into the project inbox."""
    if not text.strip():
        raise ValidationError("text must not be empty")
    if title is not None:
        _validate_title(title)
    now = datetime.now(UTC)
    resolved_title = title or f"Capture {now.strftime('%Y-%m-%d %H:%M')}"
    filename = f"{now.strftime('%Y%m%dT%H%M%S')}-{_slugify_title(resolved_title)[:40]}.md"
    doc_rel = f"inbox/{filename}"
    fm = generate_frontmatter(
        doc_id=new_document_id(),
        project_key=project_key,
        title=resolved_title,
    )
    body = text if text.endswith("\n") else text + "\n"
    return _write_new_document(
        conn,
        workspace_root,
        project_key,
        doc_rel=doc_rel,
        full_content=fm + body,
        operation_type="capture_note",
        request_json={"title": resolved_title},
    )


def _validate_upload_filename(filename: str) -> str:
    """Validate a bare filename (no path separators, no traversal)."""
    if filename != filename.strip():
        raise ValidationError("filename must not have leading or trailing whitespace")
    name = filename
    if not name:
        raise ValidationError("filename must not be empty")
    if "/" in name or "\\" in name or name in (".", ".."):
        raise ValidationError(f"filename must be a bare filename, not a path: {filename!r}")
    if name.startswith(".") or any(
        ord(character) < 32 or ord(character) == 127 for character in name
    ):
        raise ValidationError("filename must not be hidden or contain control characters")
    if len(name.encode("utf-8")) > MAX_UPLOAD_FILENAME_BYTES:
        raise ValidationError(
            f"filename exceeds the {MAX_UPLOAD_FILENAME_BYTES}-byte filesystem limit"
        )
    return name


def _validate_upload_folder_path(folder_path: str) -> str:
    """Validate folder_path is under library/ (uploads always land there)."""
    folder_path = _canonical_folder_path(folder_path, allow_empty=True)
    if not folder_path:
        return "library"
    first_segment = folder_path.split("/", 1)[0]
    if first_segment != "library":
        msg = f"folder_path {folder_path!r} must be under library/ — uploads always land there"
        raise UnknownFolderError(msg, details={"allowed_folders": ["library"]})
    return folder_path


def _canonical_folder_path(folder_path: str, *, allow_empty: bool) -> str:
    """Return one unambiguous relative POSIX folder path."""
    if folder_path != folder_path.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in folder_path
    ):
        raise ValidationError(
            "folder_path must not have surrounding whitespace or control characters"
        )
    cleaned = folder_path.strip()
    if not cleaned:
        if allow_empty:
            return ""
        raise UnknownFolderError(
            "folder_path must start with a role folder",
            details={"allowed_folders": list(CREATABLE_FOLDERS)},
        )
    if (
        cleaned.startswith("/")
        or cleaned.endswith("/")
        or "\\" in cleaned
        or "//" in cleaned
        or any(part in (".", "..") for part in PurePosixPath(cleaned).parts)
        or PurePosixPath(cleaned).as_posix() != cleaned
    ):
        raise ValidationError("folder_path must be a canonical relative POSIX path")
    return cleaned


def upload_metadata_path(content_path: str) -> str:
    """Return the sidecar path for an uploaded file (extension replaced, not appended).

    Public because file discovery (``core.files``) has to run this mapping in
    reverse to tell a Lattice-generated sidecar apart from a user-authored
    ``.json`` document that merely shares a stem.
    """
    path = PurePosixPath(content_path)
    if path.suffix.lower() == ".json":
        return f"{content_path}.metadata.json"
    return path.with_suffix(".json").as_posix()


class UploadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    snapshot_id: str
    path: str
    metadata_path: str
    folder: str | None = None
    sha256: str
    size_bytes: int
    mime_type: str | None = None


def _write_uploaded_file(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    filename: str,
    raw: bytes,
    folder_path: str,
    mime_type: str | None,
    metadata: JsonObject,
    operation_type: str,
    project_lock_held: bool = False,
    terminal_operation_id: str | None = None,
    image_policy: ImagePolicy | None = None,
) -> UploadResult:
    """Shared machinery: fail-closed collision check, write + snapshot + oplog.

    Used by both the one-shot ``upload_library_file`` and
    ``finalize_library_file_upload`` (chunked path) so their on-disk result
    is identical regardless of how the bytes arrived.

    Raster uploads are normalized to the storage policy *before* anything is
    hashed or recorded, so the sidecar, the snapshot, and the operation log
    all describe the bytes that were actually stored. The full-resolution
    original is deliberately not retained: no transport in front of this
    server can carry one, and keeping it would defeat the normalization.
    """
    filename = _validate_upload_filename(filename)
    folder_path = _validate_upload_folder_path(folder_path)
    mime_type = _validated_mime_type(mime_type)
    metadata = _validated_metadata(metadata)

    compression = compress_image_for_storage(
        raw, mime_type=mime_type, policy=image_policy or ImagePolicy()
    )
    raw = compression.data
    if compression.changed:
        mime_type = compression.mime_type
    doc_rel = f"{folder_path}/{filename}"
    metadata_rel = upload_metadata_path(doc_rel)

    project_dir = contained_project_root(workspace_root, project_key)
    target_file = _validate_path_safe(
        workspace_root, project_key, doc_rel, extensions_denylist=BLOCKED_UPLOAD_EXTENSIONS
    )
    metadata_file = _validate_path_safe(
        workspace_root, project_key, metadata_rel, extensions_denylist=BLOCKED_UPLOAD_EXTENSIONS
    )

    lock_context = (
        contextlib.nullcontext()
        if project_lock_held
        else acquire_project_lock(project_dir, project_key)
    )
    with lock_context:
        if target_file.exists() or metadata_file.exists():
            raise DocumentExistsError(
                f"A file already exists at {doc_rel}",
                details={
                    "path": doc_rel,
                    "recommended_action": "Choose a different filename or folder_path.",
                },
            )

        sha256 = hashlib.sha256(raw).hexdigest()
        now = datetime.now(UTC).isoformat()
        protected_fields: JsonObject = {
            "original_filename": filename,
            "uploaded_at": now,
            "uploaded_by_tool": operation_type,
            "sha256": sha256,
            "size_bytes": len(raw),
            "mime_type": mime_type,
        }
        if compression.changed:
            # Provenance for a stored copy that is not what the caller sent.
            protected_fields["image_compression"] = {
                "source_size_bytes": compression.original_size_bytes,
                "source_width": compression.original_width,
                "source_height": compression.original_height,
                "width": compression.width,
                "height": compression.height,
                "compressed_at": now,
            }
        merged_metadata: JsonObject = {**metadata, **protected_fields}
        metadata_text = json.dumps(merged_metadata, indent=2, sort_keys=True) + "\n"
        if len(metadata_text.encode("utf-8")) > MAX_UPLOAD_METADATA_BYTES:
            raise FileTooLargeError(
                f"Upload metadata exceeds the {MAX_UPLOAD_METADATA_BYTES}-byte limit",
                details={"max_bytes": MAX_UPLOAD_METADATA_BYTES, "scope": "metadata"},
            )

        snapshot_id = new_snapshot_id()
        snapshot_dir = create_upload_snapshot(
            project_dir,
            project_key=project_key,
            content_path=doc_rel,
            content_bytes=raw,
            metadata_path=metadata_rel,
            metadata_text=metadata_text,
            reason=operation_type,
            snapshot_id=snapshot_id,
        )

        files_published = False
        try:
            atomic_write_bytes(target_file, raw)
            atomic_write_text(metadata_file, metadata_text)
            files_published = True
            record_snapshot_in_db(
                conn,
                snapshot_id=snapshot_id,
                project_key=project_key,
                target_path=doc_rel,
                snapshot_dir=str(snapshot_dir),
                reason=operation_type,
                commit=False,
            )
            op_id = record_operation(
                conn,
                project_key=project_key,
                operation_type=operation_type,
                tool_name=operation_type,
                target_path=doc_rel,
                request_json={
                    "filename": filename,
                    "folder_path": folder_path,
                    "metadata_path": metadata_rel,
                    "size_bytes": len(raw),
                    "mime_type": mime_type,
                    "sha256": sha256,
                },
                after_sha256=sha256,
                snapshot_id=snapshot_id,
                state=OP_APPLIED,
                commit=False,
            )
            if terminal_operation_id is not None:
                mark_operation_state(
                    conn,
                    terminal_operation_id,
                    OP_APPLIED,
                    commit=False,
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            # Both targets were proven absent while holding the project lock.
            # Compensate any partial pair or durable-bookkeeping failure so a
            # failed call never leaves content without its audit records.
            target_file.unlink(missing_ok=True)
            metadata_file.unlink(missing_ok=True)
            try:
                shutil.rmtree(snapshot_dir)
            except OSError as cleanup_error:
                logger.error(
                    "Upload rollback could not remove its snapshot (type=%s, published=%s)",
                    type(cleanup_error).__name__,
                    files_published,
                )
            raise

    return UploadResult(
        operation_id=op_id,
        snapshot_id=snapshot_id,
        path=doc_rel,
        metadata_path=metadata_rel,
        folder=folder_of(doc_rel),
        sha256=sha256,
        size_bytes=len(raw),
        mime_type=mime_type,
    )


def upload_library_file(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    filename: str,
    content_base64: str,
    folder_path: str = "library",
    mime_type: str | None = None,
    metadata: JsonObject | None = None,
    image_policy: ImagePolicy | None = None,
) -> UploadResult:
    """Upload a binary file into library/, with a ``{stem}.json`` metadata sidecar.

    Always lands under ``library/``; ``folder_path`` may nest below it, like
    ``create_document``. Fails closed on a filename collision — no silent
    overwrite. Scripts/executables are blocked by extension
    (``BLOCKED_UPLOAD_EXTENSIONS``); the decoded payload is capped at
    ``MAX_CHUNK_BYTES`` since this whole call has to fit in one tool call —
    a file too large for that needs ``start_library_file_upload`` instead.
    The metadata sidecar is agent-authored: a few protected audit fields are
    stamped by the server (sha256, size, upload time, mime type), and any
    additional keys the caller supplies are kept as-is. Not indexed for
    search_project/list_tree (Markdown-only today).

    One-shot; for files that need to arrive in smaller pieces, see
    ``start_library_file_upload`` / ``append_upload_chunk`` /
    ``finalize_library_file_upload``.
    """
    name = _validate_upload_filename(filename)
    folder_path = _validate_upload_folder_path(folder_path)
    metadata_value = _validated_metadata(metadata)
    mime_type = _validated_mime_type(mime_type)

    max_encoded_chars = ((MAX_CHUNK_BYTES + 2) // 3) * 4
    if len(content_base64) > max_encoded_chars:
        raise FileTooLargeError(
            "Base64 payload exceeds the single-call upload limit",
            details={"max_bytes": MAX_CHUNK_BYTES, "scope": "file"},
        )
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except binascii.Error as exc:
        raise ValidationError(f"content_base64 is not valid base64: {exc}") from exc

    if len(raw) > MAX_CHUNK_BYTES:
        msg = f"File is {len(raw)} bytes, exceeds the {MAX_CHUNK_BYTES}-byte single-call limit"
        raise FileTooLargeError(
            msg,
            details={
                "size_bytes": len(raw),
                "max_bytes": MAX_CHUNK_BYTES,
                "scope": "file",
                "recommended_action": (
                    "Use start_library_file_upload/append_upload_chunk/"
                    "finalize_library_file_upload for files this large."
                ),
            },
        )

    return _write_uploaded_file(
        conn,
        workspace_root,
        project_key,
        filename=name,
        raw=raw,
        folder_path=folder_path,
        mime_type=mime_type,
        metadata=metadata_value,
        operation_type="upload_library_file",
        image_policy=image_policy,
    )


# ── Chunked upload (start / append / finalize / discard) ────────────────────


#: Chunked upload sessions use the same pending/applied/discarded/expired
#: lifecycle and 24h TTL as patch proposals (operations.py), keyed by
#: upload_id instead of a patch operation_id.
UPLOAD_SESSION_OP_TYPE: Final = "upload_session"
UPLOAD_SESSION_TTL: Final = timedelta(hours=24)


class UploadSessionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: str
    expires_at: str
    chunk_size_hint: int = MAX_CHUNK_BYTES


class ChunkAppendResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: str
    received_chunks: int
    total_chunks: int
    received_bytes: int


def _get_upload_session(conn: DbConnection, project_key: str, upload_id: str) -> OperationRecord:
    op = get_operation(conn, upload_id)
    if op is None:
        raise OperationNotFoundError(f"Upload {upload_id} not found")
    if op.operation_type != UPLOAD_SESSION_OP_TYPE:
        raise InvalidOperationError(f"Operation {upload_id} is not an upload session")
    if op.project_key != project_key:
        msg = (
            f"Upload {upload_id} belongs to project {op.project_key!r}, "
            f"not the asserted project {project_key!r}"
        )
        raise PatchProjectMismatchError(msg)
    return op


def _ensure_upload_session_active(
    conn: DbConnection,
    project_dir: Path,
    op: OperationRecord,
) -> None:
    """Raise if the session is expired or not pending; expire it in place if due."""
    if op.state == OP_PENDING and is_expired(op):
        upload_staging.remove_staging_dir(project_dir, op.id)
        mark_operation_state(conn, op.id, OP_EXPIRED)
        raise PatchExpiredError(
            f"Upload {op.id} expired; upload sessions are valid for 24 h. Start a new upload.",
            details={"expires_at": op.expires_at},
        )
    if op.state != OP_PENDING:
        msg = f"Upload {op.id} has state {op.state!r}, expected pending"
        raise InvalidOperationError(msg, details={"state": op.state})


def _expire_upload_sessions(
    conn: DbConnection,
    project_dir: Path,
    project_key: str,
) -> None:
    """Expire pending upload rows and remove their bounded staging data."""
    now = datetime.now(UTC).isoformat()
    rows = conn.execute(
        """SELECT id FROM operations
           WHERE project_key = ? AND operation_type = ? AND state = ?
             AND expires_at IS NOT NULL AND expires_at < ?""",
        (project_key, UPLOAD_SESSION_OP_TYPE, OP_PENDING, now),
    ).fetchall()
    for row in rows:
        upload_staging.remove_staging_dir(project_dir, row["id"])
    sweep_expired_proposals(conn, project_key)


def _pending_upload_reservations(
    conn: DbConnection,
    project_key: str,
) -> tuple[int, int]:
    """Return active upload-session count and declared bytes, failing closed on bad rows."""
    rows = conn.execute(
        """SELECT request_json FROM operations
           WHERE project_key = ? AND operation_type = ? AND state = ?""",
        (project_key, UPLOAD_SESSION_OP_TYPE, OP_PENDING),
    ).fetchall()
    reserved_bytes = 0
    for row in rows:
        try:
            raw_payload: object = json.loads(row["request_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise InvalidOperationError("Pending upload session record is malformed") from exc
        if not isinstance(raw_payload, dict):
            raise InvalidOperationError("Pending upload session record is malformed")
        payload = cast(dict[object, object], raw_payload)
        size = payload.get("total_size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise InvalidOperationError("Pending upload session record is malformed")
        reserved_bytes += size
    return len(rows), reserved_bytes


def start_library_file_upload(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    filename: str,
    total_size: int,
    total_chunks: int,
    folder_path: str = "library",
    mime_type: str | None = None,
    metadata: JsonObject | None = None,
    expected_sha256: str | None = None,
) -> UploadSessionResult:
    """Start a chunked upload session for a library file arriving in pieces.

    Declares the file's identity and size up front; bytes then arrive via
    repeated ``append_upload_chunk`` calls (each capped at
    ``MAX_CHUNK_BYTES``) and are assembled by
    ``finalize_library_file_upload``. Multiple sessions for different files
    may be open at once — each gets its own ``upload_id`` and independent
    chunk staging area, so uploads can be interleaved freely.
    """
    name = _validate_upload_filename(filename)
    folder_path = _validate_upload_folder_path(folder_path)
    metadata_value = _validated_metadata(metadata)
    mime_type = _validated_mime_type(mime_type)
    if expected_sha256 is not None:
        if not _SHA256_RE.fullmatch(expected_sha256):
            raise ValidationError("expected_sha256 must be exactly 64 hexadecimal characters")
        expected_sha256 = expected_sha256.lower()

    if isinstance(total_chunks, bool) or total_chunks < 1:
        raise ValidationError("total_chunks must be >= 1")
    if total_chunks > MAX_UPLOAD_CHUNKS:
        raise ValidationError(
            f"total_chunks {total_chunks} exceeds the maximum of {MAX_UPLOAD_CHUNKS}",
            details={"total_chunks": total_chunks, "max_chunks": MAX_UPLOAD_CHUNKS},
        )
    if isinstance(total_size, bool) or total_size < 0:
        raise ValidationError("total_size must be >= 0")
    if total_size > MAX_UPLOAD_BYTES:
        raise FileTooLargeError(
            f"Declared total_size {total_size} exceeds the {MAX_UPLOAD_BYTES}-byte upload limit",
            details={"size_bytes": total_size, "max_bytes": MAX_UPLOAD_BYTES, "scope": "file"},
        )
    if total_size > total_chunks * MAX_CHUNK_BYTES:
        raise ValidationError(
            "Declared total_size cannot fit in total_chunks at the per-chunk size limit",
            details={
                "total_size": total_size,
                "total_chunks": total_chunks,
                "max_chunk_bytes": MAX_CHUNK_BYTES,
            },
        )

    doc_rel = f"{folder_path}/{name}"
    metadata_rel = upload_metadata_path(doc_rel)
    expires_at = (datetime.now(UTC) + UPLOAD_SESSION_TTL).isoformat()
    request_json: JsonObject = {
        "filename": name,
        "folder_path": folder_path,
        "mime_type": mime_type,
        "metadata": metadata_value,
        "total_size": total_size,
        "total_chunks": total_chunks,
        "expected_sha256": expected_sha256,
    }
    upload_id = new_proposal_id()
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        _expire_upload_sessions(conn, project_dir, project_key)
        target_file = _validate_path_safe(
            workspace_root,
            project_key,
            doc_rel,
            extensions_denylist=BLOCKED_UPLOAD_EXTENSIONS,
        )
        metadata_file = _validate_path_safe(
            workspace_root,
            project_key,
            metadata_rel,
            extensions_denylist=BLOCKED_UPLOAD_EXTENSIONS,
        )
        if target_file.exists() or metadata_file.exists():
            raise DocumentExistsError(
                f"A file already exists at {doc_rel}",
                details={
                    "path": doc_rel,
                    "recommended_action": "Choose a different filename or folder_path.",
                },
            )
        duplicate = conn.execute(
            """SELECT id FROM operations
               WHERE project_key = ? AND operation_type = ? AND state = ?
                 AND target_path = ?
               LIMIT 1""",
            (project_key, UPLOAD_SESSION_OP_TYPE, OP_PENDING, doc_rel),
        ).fetchone()
        if duplicate is not None:
            raise DocumentExistsError(
                f"An upload is already pending for {doc_rel}",
                details={
                    "path": doc_rel,
                    "recommended_action": (
                        "Finalize or discard the existing upload before starting another."
                    ),
                },
            )
        pending_sessions, reserved_bytes = _pending_upload_reservations(conn, project_key)
        if pending_sessions >= MAX_PENDING_UPLOAD_SESSIONS_PER_PROJECT:
            raise ValidationError(
                "Too many pending upload sessions for this project",
                details={"max_pending_sessions": MAX_PENDING_UPLOAD_SESSIONS_PER_PROJECT},
            )
        if reserved_bytes + total_size > MAX_PENDING_UPLOAD_BYTES_PER_PROJECT:
            raise FileTooLargeError(
                "Pending uploads exceed the project staging reservation limit",
                details={
                    "reserved_bytes": reserved_bytes,
                    "requested_bytes": total_size,
                    "max_bytes": MAX_PENDING_UPLOAD_BYTES_PER_PROJECT,
                    "scope": "project_upload_staging",
                },
            )
        record_operation(
            conn,
            project_key=project_key,
            operation_type=UPLOAD_SESSION_OP_TYPE,
            tool_name="start_library_file_upload",
            target_path=doc_rel,
            request_json=request_json,
            state=OP_PENDING,
            expires_at=expires_at,
            operation_id=upload_id,
        )
    return UploadSessionResult(upload_id=upload_id, expires_at=expires_at)


def append_upload_chunk(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    upload_id: str,
    chunk_index: int,
    chunk_base64: str,
) -> ChunkAppendResult:
    """Append one chunk of a pending upload session.

    Idempotent per ``chunk_index``: resending the same index overwrites it,
    so a retried chunk is safe. Enforces ``MAX_CHUNK_BYTES`` per chunk and
    the session's declared ``total_size`` cumulatively.
    """
    max_encoded_chars = ((MAX_CHUNK_BYTES + 2) // 3) * 4
    if len(chunk_base64) > max_encoded_chars:
        raise FileTooLargeError(
            "Base64 chunk exceeds the per-chunk upload limit",
            details={"max_bytes": MAX_CHUNK_BYTES, "scope": "chunk"},
        )
    try:
        raw = base64.b64decode(chunk_base64, validate=True)
    except binascii.Error as exc:
        raise ValidationError(f"chunk_base64 is not valid base64: {exc}") from exc

    if len(raw) > MAX_CHUNK_BYTES:
        raise FileTooLargeError(
            f"Chunk is {len(raw)} bytes, exceeds the {MAX_CHUNK_BYTES}-byte per-chunk limit",
            details={"size_bytes": len(raw), "max_bytes": MAX_CHUNK_BYTES, "scope": "chunk"},
        )

    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        op = _get_upload_session(conn, project_key, upload_id)
        _ensure_upload_session_active(conn, project_dir, op)
        total_chunks = op.request_json.get("total_chunks")
        declared_total_size = op.request_json.get("total_size")
        if not isinstance(total_chunks, int) or not isinstance(declared_total_size, int):
            raise InvalidOperationError(f"Upload {upload_id} session record is malformed")
        if not (0 <= chunk_index < total_chunks):
            raise ValidationError(f"chunk_index {chunk_index} out of range [0, {total_chunks})")

        upload_staging.write_chunk(project_dir, upload_id, chunk_index, raw)
        received = upload_staging.received_chunk_indices(project_dir, upload_id)
        received_bytes = upload_staging.staged_size_bytes(project_dir, upload_id)

        if received_bytes > declared_total_size:
            upload_staging.remove_staging_dir(project_dir, upload_id)
            mark_operation_state(conn, upload_id, OP_FAILED)
            raise FileTooLargeError(
                f"Received {received_bytes} bytes, exceeds the declared "
                f"total_size {declared_total_size}",
                details={
                    "size_bytes": received_bytes,
                    "max_bytes": declared_total_size,
                    "scope": "file",
                },
            )

    return ChunkAppendResult(
        upload_id=upload_id,
        received_chunks=len(received),
        total_chunks=total_chunks,
        received_bytes=received_bytes,
    )


def finalize_library_file_upload(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    upload_id: str,
    image_policy: ImagePolicy | None = None,
) -> UploadResult:
    """Assemble a completed chunked upload and write it (same result shape as upload_library_file).

    Fails with ``UPLOAD_INCOMPLETE`` if any chunk is missing, a plain
    ``VALIDATION_ERROR`` if the assembled size doesn't match what
    ``start_library_file_upload`` declared, and ``CONTENT_HASH_MISMATCH`` if
    an ``expected_sha256`` was declared and doesn't match — the assembled
    bytes are discarded rather than written in every failure case.
    """
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        op = _get_upload_session(conn, project_key, upload_id)
        _ensure_upload_session_active(conn, project_dir, op)
        total_chunks = op.request_json.get("total_chunks")
        total_size = op.request_json.get("total_size")
        filename = op.request_json.get("filename")
        folder_path = op.request_json.get("folder_path")
        mime_type = op.request_json.get("mime_type")
        metadata = op.request_json.get("metadata")
        expected_sha256 = op.request_json.get("expected_sha256")
        if (
            not isinstance(total_chunks, int)
            or not isinstance(total_size, int)
            or not isinstance(filename, str)
            or not isinstance(folder_path, str)
            or not isinstance(metadata, dict)
            or total_chunks < 1
            or total_chunks > MAX_UPLOAD_CHUNKS
        ):
            raise InvalidOperationError(f"Upload {upload_id} session record is malformed")

        received = upload_staging.received_chunk_indices(project_dir, upload_id)
        missing = [i for i in range(total_chunks) if i not in received]
        if missing:
            raise UploadIncompleteError(
                f"Upload {upload_id} is missing {len(missing)} of {total_chunks} chunks",
                details={
                    "missing_chunk_indices": list(missing[:50]),
                    "total_chunks": total_chunks,
                },
            )
        raw = upload_staging.assemble_chunks(project_dir, upload_id, total_chunks)

        if len(raw) != total_size:
            raise ValidationError(
                f"Assembled upload is {len(raw)} bytes, does not match declared "
                f"total_size {total_size}",
                details={"assembled_bytes": len(raw), "declared_total_size": total_size},
            )

        if isinstance(expected_sha256, str) and expected_sha256:
            actual_sha256 = hashlib.sha256(raw).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ContentHashMismatchError(
                    "Assembled upload does not match expected_sha256 — likely corrupted in transit",
                    details={
                        "expected_sha256": expected_sha256,
                        "actual_sha256": actual_sha256,
                    },
                )

        result = _write_uploaded_file(
            conn,
            workspace_root,
            project_key,
            filename=filename,
            raw=raw,
            folder_path=folder_path,
            mime_type=mime_type if isinstance(mime_type, str) else None,
            metadata=metadata,
            operation_type="finalize_library_file_upload",
            project_lock_held=True,
            terminal_operation_id=upload_id,
            image_policy=image_policy,
        )
        try:
            upload_staging.remove_staging_dir(project_dir, upload_id)
        except OSError as exc:
            # Content publication and the terminal session state already
            # succeeded. Treat redundant staging cleanup as maintenance work
            # rather than returning a false failure for a completed upload.
            logger.warning(
                "Completed upload staging cleanup failed (type=%s)",
                type(exc).__name__,
            )

    return result


class DiscardUploadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_id: str
    path: str | None = None
    state: str = OP_DISCARDED


def discard_upload(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    upload_id: str,
) -> DiscardUploadResult:
    """Abandon a pending upload session and delete its staged chunks."""
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        op = _get_upload_session(conn, project_key, upload_id)
        if op.state != OP_PENDING:
            msg = f"Upload {upload_id} has state {op.state!r}, expected pending"
            raise InvalidOperationError(msg, details={"state": op.state})
        upload_staging.remove_staging_dir(project_dir, upload_id)
        mark_operation_state(conn, upload_id, OP_DISCARDED)
        return DiscardUploadResult(upload_id=upload_id, path=op.target_path)


# ── ChatGPT file-reference upload (openai/fileParams) ────────────────────────


#: Bounds worst-case per-call work: MAX_CHATGPT_FILES_PER_CALL files, each
#: up to MAX_UPLOAD_BYTES, fetched sequentially in one tool call. Aggregate
#: byte and wall-clock ceilings prevent a valid batch from multiplying the
#: per-file limit into an availability attack.
MAX_CHATGPT_FILES_PER_CALL: Final = 20
MAX_CHATGPT_BATCH_BYTES: Final = 64 * 1024 * 1024
MAX_CHATGPT_BATCH_SECONDS: Final = 60.0


class ChatGPTFileInput(BaseModel):
    """One file reference from ChatGPT's ``openai/fileParams`` extension.

    Field shape matches ChatGPT's file-reference schema exactly (see
    product/spec-mcp.md §5.3c) — do not add, rename, or drop fields.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    download_url: str = Field(
        description="Temporary, authorized URL to fetch this file's bytes from",
        min_length=1,
        max_length=8192,
    )
    file_id: str = Field(
        description="ChatGPT's identifier for this file", min_length=1, max_length=512
    )
    mime_type: str | None = Field(
        default=None,
        description="Claimed MIME type; not trusted as authoritative",
        max_length=255,
    )
    file_name: str | None = Field(
        default=None,
        description="Suggested filename; sanitized before use",
        max_length=255,
    )


class ChatGPTFileUploadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str
    filename: str | None = None
    ok: bool
    path: str | None = None
    metadata_path: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class ChatGPTBatchUploadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[ChatGPTFileUploadResult]
    succeeded: int
    failed: int


def _normalize_mime_type(mime_type: str | None) -> str | None:
    validated = _validated_mime_type(mime_type)
    if not validated:
        return None
    normalized = validated.split(";", 1)[0].strip().lower()
    return normalized or None


def _derive_chatgpt_filename(file_id: str, file_name: str | None, mime_type: str | None) -> str:
    if file_name:
        return _validate_upload_filename(file_name)
    guessed_ext = mimetypes.guess_extension(mime_type) if mime_type else None
    return _validate_upload_filename(f"{file_id}{guessed_ext or ''}")


def upload_library_files_from_chatgpt(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    files: list[ChatGPTFileInput],
    folder_path: str = "library",
    image_policy: ImagePolicy | None = None,
) -> ChatGPTBatchUploadResult:
    """Download and store one or more ChatGPT-referenced files into library/.

    Each file's ``download_url`` is fetched directly
    (``core.remote_fetch``, SSRF-hardened, streamed, capped at
    ``MAX_UPLOAD_BYTES``) — the model never sees or reproduces the bytes,
    unlike the base64 tools. One file failing does not affect the others:
    every requested file gets its own result entry with
    ``ok``/``error_code``/``error_message``, and the response makes partial
    success explicit via ``succeeded``/``failed`` counts — this tool never
    raises for a single file's failure, only for a malformed batch (empty
    or over ``MAX_CHATGPT_FILES_PER_CALL``) or a project-level problem.
    Reuses the exact same write path as ``upload_library_file`` /
    ``finalize_library_file_upload`` (extension denylist, fail-closed
    collision, metadata sidecar, snapshot, oplog) — ``mime_type`` is stored
    as metadata only and normalized for the result, never trusted in place
    of the extension check.

    Each file's name comes from its *own* resolved reference
    (``file_name``, else ``{file_id}{guessed_ext}``), so name and bytes
    travel together in one object and cannot be crossed. This tool
    deliberately takes no caller-supplied names: matching a parallel array
    of names to resolved files would require positional identity, which the
    ``openai/fileParams`` layer does not guarantee. Use
    ``upload_library_file_from_chatgpt`` (one call per file) when the
    destination filename matters.
    """
    if not files:
        raise ValidationError("files must not be empty")
    if len(files) > MAX_CHATGPT_FILES_PER_CALL:
        raise ValidationError(
            f"Too many files in one call: {len(files)} (max {MAX_CHATGPT_FILES_PER_CALL})",
            details={"max_files": MAX_CHATGPT_FILES_PER_CALL},
        )
    folder_path = _validate_upload_folder_path(folder_path)

    results: list[ChatGPTFileUploadResult] = []
    batch_started = time.monotonic()
    downloaded_bytes = 0
    for item in files:
        normalized_mime: str | None = None
        try:
            normalized_mime = _normalize_mime_type(item.mime_type)
            filename = _derive_chatgpt_filename(item.file_id, item.file_name, normalized_mime)
            remaining_seconds = MAX_CHATGPT_BATCH_SECONDS - (time.monotonic() - batch_started)
            if remaining_seconds <= 0:
                raise DownloadTimeoutError(
                    f"Batch exceeded the {MAX_CHATGPT_BATCH_SECONDS:g}s aggregate timeout"
                )
            remaining_bytes = MAX_CHATGPT_BATCH_BYTES - downloaded_bytes
            if remaining_bytes <= 0:
                raise FileTooLargeError(
                    "Batch exhausted its aggregate download limit",
                    details={
                        "max_bytes": MAX_CHATGPT_BATCH_BYTES,
                        "scope": "batch",
                    },
                )
            file_limit = min(MAX_UPLOAD_BYTES, remaining_bytes)
            raw = fetch_remote_file(
                item.download_url,
                max_bytes=file_limit,
                total_timeout=min(DEFAULT_TOTAL_TIMEOUT, remaining_seconds),
            )
            if len(raw) > file_limit:
                raise FileTooLargeError(
                    "Download exceeded the remaining aggregate batch limit",
                    details={
                        "max_bytes": MAX_CHATGPT_BATCH_BYTES,
                        "scope": "batch",
                    },
                )
            downloaded_bytes += len(raw)
            write_result = _write_uploaded_file(
                conn,
                workspace_root,
                project_key,
                filename=filename,
                raw=raw,
                folder_path=folder_path,
                mime_type=normalized_mime,
                metadata={"chatgpt_file_id": item.file_id},
                operation_type="upload_library_files_from_chatgpt",
                image_policy=image_policy,
            )
        except LatticeError as exc:
            results.append(
                ChatGPTFileUploadResult(
                    file_id=item.file_id,
                    filename=item.file_name,
                    ok=False,
                    mime_type=normalized_mime,
                    error_code=exc.code,
                    error_message=str(exc),
                )
            )
            continue
        results.append(
            ChatGPTFileUploadResult(
                file_id=item.file_id,
                filename=filename,
                ok=True,
                path=write_result.path,
                metadata_path=write_result.metadata_path,
                mime_type=normalized_mime,
                size_bytes=write_result.size_bytes,
                sha256=write_result.sha256,
            )
        )

    succeeded = sum(1 for r in results if r.ok)
    return ChatGPTBatchUploadResult(
        results=results, succeeded=succeeded, failed=len(results) - succeeded
    )


class ChatGPTSingleUploadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str
    filename: str
    operation_id: str
    snapshot_id: str
    path: str
    metadata_path: str
    mime_type: str | None = None
    size_bytes: int
    sha256: str


def upload_library_file_from_chatgpt(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    file: ChatGPTFileInput,
    filename: str,
    folder_path: str = "library",
    image_policy: ImagePolicy | None = None,
) -> ChatGPTSingleUploadResult:
    """Download one ChatGPT-referenced file and store it under an explicitly chosen *filename*.

    This is the tool to use whenever the destination filename matters. It
    exists because **there is no stable file identity the model can name**:
    a parameter listed in ``openai/fileParams`` is populated entirely by the
    ChatGPT host at resolution time, so the model never sees ``file_id`` or
    ``download_url`` when it writes the tool call and cannot bind a chosen
    name to a specific file in a sibling argument. A batch tool taking a
    parallel array of names could therefore only ever match names to files
    *by position*, which is an undocumented property of the resolution
    layer, not a guarantee — and mismatched names on a photo set are silent,
    plausible-looking corruption, not a visible error.

    One file per call sidesteps the mapping problem entirely: the single
    resolved file and the single requested ``filename`` are unambiguously
    related, whatever the host does to argument ordering, transport URLs, or
    completion order. Upload several files by making several calls; they are
    independent and safe to issue in parallel, since each takes the project
    lock only for its own write.

    ``file.file_name`` (ChatGPT's suggested name) is deliberately ignored
    here — ``filename`` is the caller's decision — but ``file.file_id`` is
    recorded in the metadata sidecar and echoed in the result so the stored
    file remains traceable to its origin. Bytes go through exactly the same
    ingestion path as every other upload tool (``_write_uploaded_file``:
    extension denylist, fail-closed collision, sidecar, snapshot, oplog).

    Unlike the batch tool, a failure here is a plain tool error: with one
    file there is no partial success to report.
    """
    filename = _validate_upload_filename(filename)
    folder_path = _validate_upload_folder_path(folder_path)
    normalized_mime = _normalize_mime_type(file.mime_type)

    # Reject a blocked extension before spending a download on bytes that
    # `_write_uploaded_file` would refuse anyway.
    _validate_path_safe(
        workspace_root,
        project_key,
        f"{folder_path}/{filename}",
        extensions_denylist=BLOCKED_UPLOAD_EXTENSIONS,
    )

    raw = fetch_remote_file(file.download_url, max_bytes=MAX_UPLOAD_BYTES)
    write_result = _write_uploaded_file(
        conn,
        workspace_root,
        project_key,
        filename=filename,
        raw=raw,
        folder_path=folder_path,
        mime_type=normalized_mime,
        metadata={"chatgpt_file_id": file.file_id},
        operation_type="upload_library_file_from_chatgpt",
        image_policy=image_policy,
    )
    return ChatGPTSingleUploadResult(
        file_id=file.file_id,
        filename=filename,
        operation_id=write_result.operation_id,
        snapshot_id=write_result.snapshot_id,
        path=write_result.path,
        metadata_path=write_result.metadata_path,
        mime_type=normalized_mime,
        size_bytes=write_result.size_bytes,
        sha256=write_result.sha256,
    )


# ── Archive lifecycle ────────────────────────────────────────────────────────


class ArchiveResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    snapshot_id: str
    path: str
    archived_path: str
    document_sha256: str
    index_error: str | None = None


def archive_document(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    path: str,
) -> ArchiveResult:
    """Archive a document: ``status: archived`` plus a move to ``archive/<path>``."""
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        target_file = _validate_path_safe(workspace_root, project_key, path)
        reconcile_document(conn, workspace_root, project_key, path)
        if not target_file.is_file():
            raise DocumentNotFoundError(f"Document not found: {path}")
        if path == SPINE_FILENAME:
            raise CannotArchiveSpineError("The spine cannot be archived")
        if is_archived_path(path):
            raise DocumentArchivedError(f"{path} is already under archive/")
        content = target_file.read_text(encoding="utf-8")
        parsed = parse_document_content(content, project_key=project_key, path=path)
        if parsed.status == "archived":
            raise DocumentArchivedError(f"{path} is already archived")

        archived_rel = archive_path_for(path)
        archived_file = _validate_path_safe(workspace_root, project_key, archived_rel)
        if archived_file.exists():
            raise PathExistsError(
                f"Archive target already exists: {archived_rel}",
                details={"archived_path": archived_rel},
            )

        new_content = _set_status(content, "archived")
        new_sha256 = compute_sha256(new_content)

        snapshot_id = new_snapshot_id()
        snapshot_dir = create_snapshot(
            project_dir,
            project_key=project_key,
            target_path=path,
            before_content=content,
            after_content=None,
            reason="archive_document",
            snapshot_id=snapshot_id,
        )
        try:
            atomic_write_text(archived_file, new_content)
            target_file.unlink()
            record_snapshot_in_db(
                conn,
                snapshot_id=snapshot_id,
                project_key=project_key,
                target_path=path,
                snapshot_dir=str(snapshot_dir),
                reason="archive_document",
                commit=False,
            )
            op_id = record_operation(
                conn,
                project_key=project_key,
                operation_type="archive_document",
                tool_name="archive_document",
                target_path=path,
                request_json={"archived_path": archived_rel},
                base_sha256=parsed.sha256,
                after_sha256=new_sha256,
                snapshot_id=snapshot_id,
                state=OP_APPLIED,
                commit=False,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            atomic_write_text(target_file, content)
            archived_file.unlink(missing_ok=True)
            try:
                shutil.rmtree(snapshot_dir)
            except OSError as cleanup_error:
                logger.error(
                    "Archive rollback could not remove its snapshot (type=%s)",
                    type(cleanup_error).__name__,
                )
            raise

        removal_error = _remove_from_index_after_write(conn, project_key, path)
        reindex_error = _reindex_after_write(
            conn,
            workspace_root,
            project_key,
            archived_file,
        )
        index_error = _combine_index_errors(removal_error, reindex_error)

    return ArchiveResult(
        operation_id=op_id,
        snapshot_id=snapshot_id,
        path=path,
        archived_path=archived_rel,
        document_sha256=new_sha256,
        index_error=index_error,
    )


def unarchive_document(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    archived_path: str,
) -> ArchiveResult:
    """Reverse an archive: move back to the mirror origin with ``status: active``."""
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        archived_file = _validate_path_safe(workspace_root, project_key, archived_path)
        reconcile_document(conn, workspace_root, project_key, archived_path)
        if not archived_file.is_file():
            raise DocumentNotFoundError(f"Document not found: {archived_path}")
        origin_rel = origin_path_for(archived_path)
        origin_file = _validate_path_safe(workspace_root, project_key, origin_rel)
        if origin_file.exists():
            raise PathExistsError(
                f"Cannot unarchive: {origin_rel} already exists",
                details={"path": origin_rel},
            )

        content = archived_file.read_text(encoding="utf-8")
        old_sha256 = compute_sha256(content)
        new_content = _set_status(content, "active")
        new_sha256 = compute_sha256(new_content)

        snapshot_id = new_snapshot_id()
        snapshot_dir = create_snapshot(
            project_dir,
            project_key=project_key,
            target_path=archived_path,
            before_content=content,
            after_content=None,
            reason="unarchive_document",
            snapshot_id=snapshot_id,
        )
        try:
            atomic_write_text(origin_file, new_content)
            archived_file.unlink()
            record_snapshot_in_db(
                conn,
                snapshot_id=snapshot_id,
                project_key=project_key,
                target_path=archived_path,
                snapshot_dir=str(snapshot_dir),
                reason="unarchive_document",
                commit=False,
            )
            op_id = record_operation(
                conn,
                project_key=project_key,
                operation_type="unarchive_document",
                tool_name="unarchive_document",
                target_path=origin_rel,
                request_json={"archived_path": archived_path},
                base_sha256=old_sha256,
                after_sha256=new_sha256,
                snapshot_id=snapshot_id,
                state=OP_APPLIED,
                commit=False,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            atomic_write_text(archived_file, content)
            origin_file.unlink(missing_ok=True)
            try:
                shutil.rmtree(snapshot_dir)
            except OSError as cleanup_error:
                logger.error(
                    "Unarchive rollback could not remove its snapshot (type=%s)",
                    type(cleanup_error).__name__,
                )
            raise

        removal_error = _remove_from_index_after_write(conn, project_key, archived_path)
        reindex_error = _reindex_after_write(
            conn,
            workspace_root,
            project_key,
            origin_file,
        )
        index_error = _combine_index_errors(removal_error, reindex_error)

    return ArchiveResult(
        operation_id=op_id,
        snapshot_id=snapshot_id,
        path=origin_rel,
        archived_path=archived_path,
        document_sha256=new_sha256,
        index_error=index_error,
    )


def _set_status(content: str, status: str) -> str:
    """Set ``status`` in the frontmatter block, adding the key if missing."""
    fm_block, body = extract_frontmatter_block(content)
    if not fm_block:
        # Unmanaged file: archive still works, status lives only in the path.
        return content
    status_pattern = re.compile(r"^status\s*:.*$", re.MULTILINE)
    if status_pattern.search(fm_block):
        new_block = status_pattern.sub(f"status: {status}", fm_block, count=1)
    else:
        lines = fm_block.split("\n")
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == "---":
                lines.insert(i, f"status: {status}")
                break
        new_block = "\n".join(lines)
    now = datetime.now(UTC).isoformat()
    return set_frontmatter_updated(new_block, now) + body


# ── Restore ──────────────────────────────────────────────────────────────────


def restore_snapshot(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    snapshot_id: str,
) -> WriteResult:
    """Restore a file from a snapshot.

    Creates a pre-restore snapshot first, then restores the target snapshot.
    Both operations are snapshot-protected and logged.
    """
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        snapshot_dir = find_snapshot_dir(project_dir, snapshot_id)
        if snapshot_dir is None:
            raise SnapshotNotFoundError(
                f"Snapshot {snapshot_id} not found for project {project_key}"
            )

        metadata = read_snapshot_metadata(snapshot_dir)
        if metadata is None or metadata.id != snapshot_id or metadata.project_key != project_key:
            raise SnapshotNotFoundError(
                f"Snapshot {snapshot_id} metadata is missing, invalid, or mismatched"
            )
        target_path_str = metadata.target_path
        if not target_path_str:
            raise SnapshotNotFoundError(f"Snapshot {snapshot_id} has no target_path")
        if metadata.operation_type in {"archive_document", "unarchive_document"}:
            raise InvalidOperationError(
                "Archive transition snapshots cannot be restored as a single file; "
                "use archive_document or unarchive_document instead",
                details={"snapshot_reason": metadata.operation_type},
            )
        if metadata.before_sha256 is None or metadata.before_size_bytes is None:
            raise SnapshotNotFoundError(f"Snapshot {snapshot_id} has no verifiable before-content")

        target_file = _validate_path_safe(workspace_root, project_key, target_path_str)
        restored_content = read_snapshot_before_content(
            snapshot_dir,
            target_path_str,
            expected_sha256=metadata.before_sha256,
            expected_size_bytes=metadata.before_size_bytes,
            max_bytes=MAX_MUTATED_MARKDOWN_BYTES,
        )
        if restored_content is None:
            msg = f"Snapshot {snapshot_id} before-content is missing or failed integrity checks"
            raise SnapshotNotFoundError(msg)
        _validate_markdown_size(restored_content)
        parse_document_content(
            restored_content,
            project_key=project_key,
            path=target_path_str,
        )

        current_content = ""
        target_existed = target_file.is_file()
        if target_existed:
            current_content = target_file.read_text(encoding="utf-8")

        before_snapshot_id = new_snapshot_id()
        before_snapshot_dir = create_snapshot(
            project_dir,
            project_key=project_key,
            target_path=target_path_str,
            before_content=current_content if target_existed else None,
            after_content=None,
            reason="pre_restore_snapshot",
            snapshot_id=before_snapshot_id,
        )
        after_sha256 = compute_sha256(restored_content)
        try:
            atomic_write_text(target_file, restored_content)
            record_snapshot_in_db(
                conn,
                snapshot_id=before_snapshot_id,
                project_key=project_key,
                target_path=target_path_str,
                snapshot_dir=str(before_snapshot_dir),
                reason="pre_restore_snapshot",
                commit=False,
            )
            op_id = record_operation(
                conn,
                project_key=project_key,
                operation_type="restore_snapshot",
                tool_name="restore_snapshot",
                target_path=target_path_str,
                request_json={
                    "restored_from_snapshot_id": snapshot_id,
                    "rollback_snapshot_id": before_snapshot_id,
                },
                base_sha256=compute_sha256(current_content) if target_existed else None,
                after_sha256=after_sha256,
                snapshot_id=before_snapshot_id,
                state=OP_APPLIED,
                commit=False,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            if target_existed:
                atomic_write_text(target_file, current_content)
            else:
                target_file.unlink(missing_ok=True)
            try:
                shutil.rmtree(before_snapshot_dir)
            except OSError as cleanup_error:
                logger.error(
                    "Restore rollback could not remove its snapshot (type=%s)",
                    type(cleanup_error).__name__,
                )
            raise

        index_error = _reindex_after_write(conn, workspace_root, project_key, target_file)

    return WriteResult(
        operation_id=op_id,
        snapshot_id=before_snapshot_id,
        restored_from_snapshot_id=snapshot_id,
        rollback_snapshot_id=before_snapshot_id,
        path=target_path_str,
        folder=folder_of(target_path_str),
        document_sha256=after_sha256,
        index_error=index_error,
    )


# ── Project creation ─────────────────────────────────────────────────────────


class CreateProjectResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    path: str
    operation_id: str
    snapshot_id: str
    seeded: list[str]


def create_project(
    conn: DbConnection,
    workspace_root: WorkspaceRoot,
    *,
    key: str,
    title: str,
) -> CreateProjectResult:
    """Create a project: registry entry + folder skeleton + seeded spine/rules.

    Seeds ``spine.md`` and ``rules/00-project.md`` from
    ``system/templates/`` (installed from product/contract by the bootstrap
    script), substituting ``{{project_title}}``.
    """
    project_key = validate_project_key(key)
    _validate_title(title)

    with acquire_workspace_lock(workspace_root):
        registry = load_registry(workspace_root)
        if str(project_key) in registry:
            raise PathExistsError(
                f"Project {key!r} already exists",
                details={"key": key},
            )
        project_dir = contained_path(workspace_root, f"projects/{project_key}")
        if project_dir.exists():
            raise PathExistsError(f"Project directory already exists: projects/{project_key}")

        seeded = [SPINE_FILENAME, "rules/00-project.md"]
        spine_content = _seed_from_template(
            workspace_root,
            template_name="spine.md",
            project_key=str(project_key),
            title=title,
            edit_policy=None,
        )
        rules_content = _seed_from_template(
            workspace_root,
            template_name="project-rules.md",
            project_key=str(project_key),
            title=title,
            edit_policy="ask-human",
        )
        rules_rel = "rules/00-project.md"

        entry = ProjectEntry(
            key=str(project_key),
            title=title,
            path=f"projects/{project_key}",
            status="active",
        )
        updated_registry = dict(registry)
        updated_registry[str(project_key)] = entry
        registry_file = contained_path(workspace_root, "system/projects.yml")
        before_files = (
            {"system/projects.yml": registry_file.read_text(encoding="utf-8")}
            if registry_file.is_file()
            else {}
        )
        after_files = {
            f"projects/{project_key}/{SPINE_FILENAME}": spine_content,
            f"projects/{project_key}/{rules_rel}": rules_content,
            "system/projects.yml": serialize_registry(updated_registry),
        }

        # Snapshot the intended transition before publishing either the new
        # project folder or its registry entry.
        snapshot_id = new_snapshot_id()
        snapshot_dir = create_global_snapshot(
            workspace_root,
            snapshot_id=snapshot_id,
            operation_type="create_project",
            target_project_key=str(project_key),
            reason="create_project",
            before_files=before_files,
            after_files=after_files,
        )

        projects_root = contained_path(workspace_root, "projects")
        projects_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        projects_root.chmod(0o700)
        staging_base = contained_path(workspace_root, ".lattice/project-staging")
        staging_base.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging_base.chmod(0o700)
        staging_dir = contained_path(staging_base, f"{project_key}-{snapshot_id}")
        staging_dir.mkdir(mode=0o700)
        published = False
        try:
            for sub in PROJECT_DIRECTORIES:
                contained_path(staging_dir, sub).mkdir(
                    mode=0o700,
                    parents=True,
                    exist_ok=True,
                )
            atomic_write_text(contained_path(staging_dir, SPINE_FILENAME), spine_content)
            atomic_write_text(contained_path(staging_dir, rules_rel), rules_content)

            # Directory rename publishes a complete skeleton atomically.
            staging_dir.replace(project_dir)
            published = True
        except BaseException:
            if published:
                _withdraw_unpublished_project(project_dir, staging_dir)
            else:
                shutil.rmtree(staging_dir, ignore_errors=True)
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            raise

        # Durable bookkeeping is committed while the project is still absent
        # from the registry. Project-scoped callers therefore cannot enter the
        # new tree before a failure can be compensated.
        try:
            record_snapshot_in_db(
                conn,
                snapshot_id=snapshot_id,
                # Global snapshots are not readable through the project-file
                # snapshot tools; keep them out of project snapshot listings.
                project_key="",
                target_path=None,
                snapshot_dir=str(snapshot_dir),
                reason="create_project",
                commit=False,
            )
            op_id = record_operation(
                conn,
                project_key=str(project_key),
                operation_type="create_project",
                tool_name="create_project",
                request_json={"key": str(project_key), "title": title},
                snapshot_id=snapshot_id,
                state=OP_APPLIED,
                commit=False,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            if _withdraw_unpublished_project(project_dir, staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)
                shutil.rmtree(snapshot_dir, ignore_errors=True)
            raise

        # Registry publication is the visibility boundary. atomic_write_text
        # may replace the file successfully and then report a directory-fsync
        # failure, so inspect the source of truth before compensating.
        try:
            save_registry(workspace_root, updated_registry)
        except (LatticeError, OSError):
            current_registry = _load_registry_for_publication_check(workspace_root)
            if current_registry == updated_registry:
                logger.warning(
                    "Project registry publication reported failure after the intended "
                    "registry became visible; treating project creation as committed"
                )
            elif current_registry == registry:
                try:
                    conn.execute(
                        "DELETE FROM operations WHERE id = ? AND operation_type = ?",
                        (op_id, "create_project"),
                    )
                    conn.execute(
                        "DELETE FROM snapshots WHERE id = ? AND snapshot_dir = ?",
                        (snapshot_id, str(snapshot_dir)),
                    )
                    conn.commit()
                except sqlite3.Error:
                    conn.rollback()
                    logger.critical(
                        "Could not compensate hidden project bookkeeping; preserving "
                        "the unpublished project tree and snapshot"
                    )
                    raise
                if _withdraw_unpublished_project(project_dir, staging_dir):
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    shutil.rmtree(snapshot_dir, ignore_errors=True)
                raise
            else:
                logger.critical(
                    "Project registry state is ambiguous after publication failure; "
                    "preserving the project tree, snapshot, and bookkeeping"
                )
                raise
        for rel in seeded:
            _reindex_after_write(conn, workspace_root, str(project_key), project_dir / rel)

    return CreateProjectResult(
        key=entry.key,
        title=entry.title,
        path=entry.path,
        operation_id=op_id,
        snapshot_id=snapshot_id,
        seeded=seeded,
    )


def _withdraw_unpublished_project(project_dir: Path, staging_dir: Path) -> bool:
    """Move a hidden project out of ``projects/`` before recursive cleanup.

    Registry publication happens only after durable bookkeeping, so a project
    reaching this helper was never visible to scoped callers. Renaming first
    also ensures cleanup never recursively deletes through a live project path.
    """
    try:
        if project_dir.exists():
            project_dir.replace(staging_dir)
        return True
    except OSError as exc:
        logger.critical(
            "Could not withdraw an unpublished project tree; preserving it (type=%s)",
            type(exc).__name__,
        )
        return False


def _load_registry_for_publication_check(
    workspace_root: WorkspaceRoot,
) -> dict[str, ProjectEntry] | None:
    """Read registry state after an ambiguous atomic publication failure."""
    try:
        return load_registry(workspace_root)
    except LatticeError as exc:
        logger.critical(
            "Could not verify project registry after publication failure (type=%s)",
            type(exc).__name__,
        )
        return None


def _validate_title(title: str) -> None:
    """Reject empty or excessively large user-facing titles."""
    if not title.strip():
        raise ValidationError("title must not be empty")
    if len(title) > MAX_TITLE_CHARS:
        raise ValidationError(
            f"title exceeds the {MAX_TITLE_CHARS}-character limit",
            details={"max_chars": MAX_TITLE_CHARS},
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in title):
        raise ValidationError("title must not contain control characters")


def _seed_from_template(
    workspace_root: Path,
    *,
    template_name: str,
    project_key: str,
    title: str,
    edit_policy: str | None,
) -> str:
    """Render a seed document from a workspace template.

    The template's own frontmatter (title/status placeholders) is replaced by
    generated managed frontmatter; the body keeps the template's guidance
    with ``{{project_title}}`` substituted.
    """
    templates_dir = contained_path(workspace_root, "system/templates")
    template_path = contained_path(templates_dir, template_name)
    if template_path.is_file():
        raw = template_path.read_text(encoding="utf-8")
        _fm, body = extract_frontmatter_block(raw)
        body = body.replace("{{project_title}}", title)
    else:
        body = f"# {title}\n"
    fm = generate_frontmatter(
        doc_id=new_document_id(),
        project_key=project_key,
        title=title,
        edit_policy=edit_policy,
    )
    return fm + body.lstrip("\n")


def registry_entry_or_none(workspace_root: WorkspaceRoot, key: str) -> ProjectEntry | None:
    return load_registry(workspace_root).get(key)
