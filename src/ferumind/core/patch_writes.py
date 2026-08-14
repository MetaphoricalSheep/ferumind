"""Propose → apply: the guarded edit transaction for managed Markdown.

A ``propose_*`` call writes no bytes. It reads the target, checks the caller's
hash guards, prepares the resulting document, and records a *pending*
operation bound to project + path + base hash with a 24 h TTL. Only
:func:`apply_patch` writes, and only after revalidating that binding, the TTL,
and the on-disk content it was promised — snapshotting first, and committing
the file write, the snapshot row, the apply record, and the proposal's
retirement so a replay cannot observe a half-published state.

Hard refusals raised from here (the closed list, 00 principle 1): archived
targets (``DOCUMENT_ARCHIVED``), protected frontmatter identity keys
(``FRONTMATTER_PROTECTED``), out-of-project paths (``WORKSPACE_MISMATCH``).
Edit policy and frozen structure are echoed to the agent, never blocked.
"""

from __future__ import annotations

import difflib
import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ferumind.core.document_map import hash_line_range, split_document_lines
from ferumind.core.documents import ParsedDocument, compute_sha256, parse_document_content
from ferumind.core.edit_targets import ExactEdit, InsertAnchor
from ferumind.core.errors import (
    DocumentArchivedError,
    DocumentNotFoundError,
    FrontmatterInvalidError,
    FrontmatterProtectedError,
    FrontmatterRequiredError,
    InvalidOperationError,
    OperationNotFoundError,
    PatchConflictError,
    PatchExpiredError,
    PatchProjectMismatchError,
    TargetHashMismatchError,
    WorkspaceMismatchError,
)
from ferumind.core.file_io import atomic_write_text
from ferumind.core.folders import folder_of, is_archived_path
from ferumind.core.frontmatter import (
    REQUIRED_FRONTMATTER_KEYS,
    extract_frontmatter_block,
    is_managed_markdown,
    parse_frontmatter,
    set_frontmatter_updated,
    validate_description,
)
from ferumind.core.locks import acquire_project_lock
from ferumind.core.operations import (
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
    record_operation,
    record_proposal,
)
from ferumind.core.patches import (
    PreparedPatch,
    prepare_exact_replace_patch,
    prepare_frontmatter_patch,
    prepare_insert_patch,
    prepare_multi_edit_patch,
    prepare_range_patch,
    prepare_search_replace_patch,
    prepare_section_patch,
)
from ferumind.core.paths import contained_project_root, is_under_root
from ferumind.core.policy import PolicyEcho, policy_echo_for
from ferumind.core.reconcile import reconcile_document
from ferumind.core.snapshots import create_snapshot, new_snapshot_id, record_snapshot_in_db
from ferumind.core.types import DbConnection, JsonObject
from ferumind.core.write_common import (
    WriteResult,
    reindex_after_write,
    validate_markdown_size,
    validate_path_safe,
)

logger = logging.getLogger(__name__)

PatchMode = Literal["body", "full"]


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


def _generate_diff_text(before: str, after: str, path: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


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
    target_file = validate_path_safe(workspace_root, project_key, path)
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
    validate_markdown_size(prepared.new_full_content)
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

    # A full-file replacement is the one propose path that can drop a key
    # wholesale. Catching it here fails the proposal closed rather than
    # writing a malformed document and reporting an index error afterwards.
    try:
        validate_description(new_fm.get("description"))
    except FrontmatterInvalidError as exc:
        raise FrontmatterRequiredError(
            f"Patched content has no valid frontmatter description: {exc}"
        ) from exc

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
                f"Frontmatter key '{key}' is managed by Ferumind and cannot be edited",
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
                f"Frontmatter key '{key}' is managed by Ferumind and cannot be edited",
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
        target_file = validate_path_safe(workspace_root, project_key, target_path_str)
        if not is_under_root(target_file, project_dir):
            msg = f"Operation {operation_id} target is outside the project {project_key!r}"
            raise WorkspaceMismatchError(msg)

        # Reconcile before the hash check so an out-of-band edit both stales
        # the proposal and produces the right conflict below.
        outcome = reconcile_document(conn, workspace_root, project_key, target_path_str)

        new_content = op.request_json.get("new_content")
        if not isinstance(new_content, str):
            raise InvalidOperationError(f"Operation {operation_id} has no new_content in request")
        validate_markdown_size(new_content)
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

        index_error = reindex_after_write(conn, workspace_root, project_key, target_file)

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
