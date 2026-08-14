"""Document creation and capture: new managed Markdown, from three doors.

``create_document`` (an agent names a folder and a title), ``capture_note``
(a stray thought lands in ``inbox/``), and ``record_episode`` (what happened,
appended to this month's episode ledger). All three publish *new* text the
calling agent authored, which is why none of them goes through the guarded
propose → apply transaction in :mod:`ferumind.core.patch_writes`: that exists
to guard edits to text somebody else wrote.

Every write here is snapshot-protected and operation-logged, and the whole of
it runs under the project lock — see :func:`_write_new_document_locked` for why
the lock is split off the body rather than taken inside it.

Bounds live in :mod:`ferumind.core.write_limits`; the path/size/title guards
and :class:`~ferumind.core.write_common.WriteResult` live in
:mod:`ferumind.core.write_common`.
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from ferumind.core.documents import compute_sha256
from ferumind.core.errors import (
    DocumentArchivedError,
    DocumentExistsError,
    UnknownFolderError,
    ValidationError,
    WorkspaceMismatchError,
)
from ferumind.core.file_io import atomic_write_text
from ferumind.core.folders import CREATABLE_FOLDERS, archive_path_for, folder_of
from ferumind.core.frontmatter import (
    FrontmatterBehavior,
    generate_frontmatter,
    new_document_id,
    new_episode_id,
    validate_description,
)
from ferumind.core.locks import acquire_project_lock
from ferumind.core.operations import OP_APPLIED, record_operation
from ferumind.core.paths import PathSafetyError, contained_path, contained_project_root
from ferumind.core.reconcile import reconcile_document
from ferumind.core.snapshots import create_snapshot, new_snapshot_id, record_snapshot_in_db
from ferumind.core.types import DbConnection, JsonObject
from ferumind.core.write_common import (
    WriteResult,
    canonical_folder_path,
    reindex_after_write,
    validate_markdown_size,
    validate_path_safe,
    validate_title,
)
from ferumind.core.write_limits import (
    EPISODES_FOLDER,
    MAX_EPISODE_RELATED_PATHS,
    MAX_EPISODE_SUMMARY_CHARS,
    MAX_EPISODE_TITLE_CHARS,
)

logger = logging.getLogger(__name__)

#: ``capture_note`` and ``record_episode`` compose the whole document, so
#: neither can take a description from a caller who has not written one. The
#: server supplies a fixed structural sentence instead — and that is a
#: statement about a server-owned *file shape*, not the server deciding what
#: content means. An inbox capture is for triage and an episode ledger is for
#: chronology whatever text lands in them, which is exactly why one honest
#: sentence covers every instance.
#:
#: The alternative considered and rejected was defaulting either one to the
#: title. That is the failure FMT-01 names outright: a description that
#: restates the title costs bytes on every ``get_context`` call forever and
#: answers nothing.
CAPTURE_NOTE_DESCRIPTION: Final = (
    "Unfiled capture from a chat, held in the inbox until it is triaged into a "
    "role folder or archived."
)


def _slugify_title(title: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return normalized[:60] or "document"


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
    project_dir = contained_project_root(workspace_root, project_key)
    with acquire_project_lock(project_dir, project_key):
        return _write_new_document_locked(
            conn,
            workspace_root,
            project_key,
            _NewDocumentWrite(
                doc_rel=doc_rel,
                full_content=full_content,
                operation_type=operation_type,
                request_json=request_json,
            ),
        )


def _write_new_document_locked(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    write: _NewDocumentWrite,
) -> WriteResult:
    """:func:`_write_new_document` minus the lock; the caller must hold it.

    Split out because ``flock()`` is not re-entrant across file descriptions:
    a caller already holding the project lock would block on a nested acquire
    until the timeout and then fail, in the same process. ``record_episode``
    needs one lock spanning its create-or-append decision, so it calls this
    directly rather than nesting.
    """
    validate_markdown_size(write.full_content)
    project_dir = contained_project_root(workspace_root, project_key)
    target_file = validate_path_safe(workspace_root, project_key, write.doc_rel)

    if target_file.exists():
        raise DocumentExistsError(
            f"Document already exists: {write.doc_rel}",
            details={
                "path": write.doc_rel,
                "recommended_action": (
                    "Choose a different title/filename, or edit the existing document "
                    "with the propose/apply tools."
                ),
            },
        )
    sha256 = compute_sha256(write.full_content)
    snapshot_id = new_snapshot_id()
    snapshot_dir = create_snapshot(
        project_dir,
        project_key=project_key,
        target_path=write.doc_rel,
        before_content=None,
        after_content=write.full_content,
        reason=write.operation_type,
        snapshot_id=snapshot_id,
    )
    try:
        atomic_write_text(target_file, write.full_content)
        record_snapshot_in_db(
            conn,
            snapshot_id=snapshot_id,
            project_key=project_key,
            target_path=write.doc_rel,
            snapshot_dir=str(snapshot_dir),
            reason=write.operation_type,
            commit=False,
        )
        op_id = record_operation(
            conn,
            project_key=project_key,
            operation_type=write.operation_type,
            tool_name=write.operation_type,
            target_path=write.doc_rel,
            request_json=write.request_json,
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
    index_error = reindex_after_write(conn, workspace_root, project_key, target_file)

    return WriteResult(
        operation_id=op_id,
        snapshot_id=snapshot_id,
        path=write.doc_rel,
        folder=folder_of(write.doc_rel),
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
    description: str,
    content: str,
    status: str | None = None,
    edit_policy: str | None = None,
) -> WriteResult:
    """Create a new managed Markdown document in a role folder.

    ``folder_path`` must start with a creatable role folder (rules, canvases,
    memory, library, inbox) and may nest freely below it; ``UNKNOWN_FOLDER``
    otherwise. The filename is derived from the title.

    ``description`` is the caller's: this door is the one where an agent has
    just decided what the document is for, so it is the one place where the
    answer is genuinely known at creation time.
    """
    validate_title(title)
    description = validate_description(description)
    folder_path = canonical_folder_path(folder_path, allow_empty=False)
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
        description=description,
        behavior=FrontmatterBehavior(
            status=status or "active",
            edit_policy=edit_policy,
        ),
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
        validate_title(title)
    now = datetime.now(UTC)
    resolved_title = title or f"Capture {now.strftime('%Y-%m-%d %H:%M')}"
    filename = f"{now.strftime('%Y%m%dT%H%M%S')}-{_slugify_title(resolved_title)[:40]}.md"
    doc_rel = f"inbox/{filename}"
    fm = generate_frontmatter(
        doc_id=new_document_id(),
        project_key=project_key,
        title=resolved_title,
        description=CAPTURE_NOTE_DESCRIPTION,
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


@dataclass(frozen=True)
class _NewDocumentWrite:
    """One new-file write, grouped so the locked helper stays inside its budget."""

    doc_rel: str
    full_content: str
    operation_type: str
    request_json: JsonObject


@dataclass(frozen=True)
class _EpisodeAppend:
    """One append to an existing month file, resolved before the write begins."""

    month_rel: str
    target_file: Path
    section: str
    episode_id: str
    request_json: JsonObject


#: ``ep_`` + 12 hex, matching :func:`new_episode_id`. Format-checked only:
#: ``related_episode_id`` is never resolved, because the target may live in an
#: archived month file and resolving it would mean a workspace-wide scan on
#: every write. A dangling reference is not an error.
_EPISODE_ID_PATTERN: Final = re.compile(r"^ep_[0-9a-f]{12}$")


class EpisodeDraft(BaseModel):
    """The meaning an agent supplies for one episode.

    Everything the record needs to be *trustworthy* — the date, the id, the
    path, the month — is supplied by the server and is deliberately absent
    here. A model is worst at exactly the two fields a historical record
    depends on, so neither is an input.
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    summary: str
    related_paths: tuple[str, ...] = ()
    related_episode_id: str | None = None


class EpisodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str
    snapshot_id: str | None = None
    path: str
    folder: str | None = None
    document_sha256: str | None = None
    index_error: str | None = None
    episode_id: str
    month_file_created: bool


def _episode_now() -> datetime:
    """Return the server's current UTC time.

    A module-level seam so tests can freeze the clock, and — more importantly
    — so no date can enter through the public API. ``record_episode`` takes no
    date, month, or filename argument: that is not a convenience withheld, it
    is the reason the month path has no traversal surface and the reason a
    model that believes the wrong date cannot write one into the permanent
    record.
    """
    return datetime.now(UTC)


def _validate_episode_text(value: str, *, field: str, limit: int) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValidationError(f"{field} must not be empty or whitespace-only")
    if len(stripped) > limit:
        raise ValidationError(
            f"{field} is {len(stripped)} characters; maximum is {limit}",
            details={"field": field, "max_chars": limit},
        )
    return stripped


def _validate_episode_related_paths(
    workspace_root: Path,
    project_key: str,
    related_paths: Sequence[str],
) -> tuple[str, ...]:
    """Containment-check every caller-supplied path, preserving order.

    ``related_paths`` is the only caller-controlled path input on this tool,
    so each entry goes through the same validator every other write uses.
    Traversal, absolute paths, and symlinks that leave the project are
    rejected there, and a path in another project fails containment against
    this project's root.
    """
    if len(related_paths) > MAX_EPISODE_RELATED_PATHS:
        raise ValidationError(
            f"related_paths has {len(related_paths)} entries; "
            f"maximum is {MAX_EPISODE_RELATED_PATHS}",
            details={"max_entries": MAX_EPISODE_RELATED_PATHS},
        )
    validated: list[str] = []
    for raw in related_paths:
        candidate = raw.strip()
        if not candidate:
            raise ValidationError("related_paths entries must not be empty")
        project_root = contained_project_root(workspace_root, project_key)
        try:
            resolved = contained_path(project_root, candidate)
        except PathSafetyError as exc:
            raise WorkspaceMismatchError(
                f"related_paths entry {candidate!r} is outside project {project_key!r}",
                details={"path": candidate},
            ) from exc
        validated.append(resolved.relative_to(project_root).as_posix())
    return tuple(dict.fromkeys(validated))


def _validate_episode_draft(
    workspace_root: Path,
    project_key: str,
    draft: EpisodeDraft,
) -> EpisodeDraft:
    follows = draft.related_episode_id
    if follows is not None and not _EPISODE_ID_PATTERN.match(follows):
        raise ValidationError(
            f"related_episode_id {follows!r} is not an episode id (expected ep_ + 12 hex)",
            details={"related_episode_id": follows},
        )
    return EpisodeDraft(
        title=_validate_episode_text(draft.title, field="title", limit=MAX_EPISODE_TITLE_CHARS),
        summary=_validate_episode_text(
            draft.summary, field="summary", limit=MAX_EPISODE_SUMMARY_CHARS
        ),
        related_paths=_validate_episode_related_paths(
            workspace_root, project_key, draft.related_paths
        ),
        related_episode_id=follows,
    )


def _render_episode(draft: EpisodeDraft, *, episode_id: str, now: datetime) -> str:
    """Render one episode as an addressable ``##`` section.

    The date is in the heading so a human reading the raw file in vim sees
    chronology without decoding anything, and the id is on its own line so an
    exact-string search finds a follow-up's target. Absent fields emit no
    line at all — an empty ``Related:`` is noise in a file nobody rewrites.
    """
    lines = [
        f"## {now:%Y-%m-%d} — {draft.title}",
        "",
        f"- ID: {episode_id}",
    ]
    if draft.related_paths:
        lines.append(f"- Related: {', '.join(draft.related_paths)}")
    if draft.related_episode_id is not None:
        lines.append(f"- Follows: {draft.related_episode_id}")
    lines.extend(["", draft.summary, ""])
    return "\n".join(lines)


def _refuse_archived_episode_month(workspace_root: Path, project_key: str, month_rel: str) -> None:
    """Refuse when this month's file exists only under ``archive/``.

    Creating a fresh live file over an archived month strands the archived
    history: ``unarchive_document`` would later fail with ``PATH_EXISTS`` and
    the older episodes would have no way back. Refusing here follows the
    existing closed list of hard refusals rather than inventing a new code.
    """
    archived_rel = archive_path_for(month_rel)
    archived_file = validate_path_safe(workspace_root, project_key, archived_rel)
    if archived_file.is_file():
        raise DocumentArchivedError(
            f"This month's episode file is archived at {archived_rel}",
            details={
                "path": month_rel,
                "archived_path": archived_rel,
                "recommended_action": (
                    f"Call unarchive_document(archived_path={archived_rel!r}) and retry."
                ),
            },
        )


def _append_episode_to_month(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    append: _EpisodeAppend,
) -> EpisodeResult:
    """Append one episode to an existing month file, snapshot-protected.

    Reconciles first so a hand-edited month file is reindexed and its stale
    proposals marked, and so the append lands on the real on-disk bytes
    rather than a cached copy. The append point is end-of-file, not "after
    the last ``##``": a file with unexpected structure still gets a valid
    append rather than a guess.
    """
    project_dir = contained_project_root(workspace_root, project_key)
    reconcile_document(conn, workspace_root, project_key, append.month_rel)
    before_content = append.target_file.read_text(encoding="utf-8")
    after_content = f"{before_content.rstrip()}\n\n{append.section}"
    validate_markdown_size(after_content)
    after_sha256 = compute_sha256(after_content)

    snapshot_id = new_snapshot_id()
    snapshot_dir = create_snapshot(
        project_dir,
        project_key=project_key,
        target_path=append.month_rel,
        before_content=before_content,
        after_content=after_content,
        reason="record_episode",
        snapshot_id=snapshot_id,
    )
    try:
        atomic_write_text(append.target_file, after_content)
        record_snapshot_in_db(
            conn,
            snapshot_id=snapshot_id,
            project_key=project_key,
            target_path=append.month_rel,
            snapshot_dir=str(snapshot_dir),
            reason="record_episode",
            commit=False,
        )
        op_id = record_operation(
            conn,
            project_key=project_key,
            operation_type="record_episode",
            tool_name="record_episode",
            target_path=append.month_rel,
            request_json=append.request_json,
            base_sha256=compute_sha256(before_content),
            after_sha256=after_sha256,
            snapshot_id=snapshot_id,
            state=OP_APPLIED,
            commit=False,
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        with contextlib.suppress(OSError):
            atomic_write_text(append.target_file, before_content)
        try:
            shutil.rmtree(snapshot_dir)
        except OSError as cleanup_error:
            logger.error(
                "Episode-append rollback could not remove its snapshot (type=%s)",
                type(cleanup_error).__name__,
            )
        raise
    index_error = reindex_after_write(conn, workspace_root, project_key, append.target_file)
    return EpisodeResult(
        operation_id=op_id,
        snapshot_id=snapshot_id,
        path=append.month_rel,
        folder=folder_of(append.month_rel),
        document_sha256=after_sha256,
        index_error=index_error,
        episode_id=append.episode_id,
        month_file_created=False,
    )


def record_episode(
    conn: DbConnection,
    workspace_root: Path,
    project_key: str,
    *,
    draft: EpisodeDraft,
) -> EpisodeResult:
    """Record one episode into this month's ``memory/episodes/YYYY-MM.md``.

    The agent supplies meaning; Ferumind supplies the month, the path, the
    timestamp, the id, the snapshot, and the operation-log entry. A direct
    write rather than propose/apply, consistent with ``create_document`` and
    ``capture_note``: propose/apply guards edits to text somebody else wrote,
    and this is new text the calling agent authored, appended to the end of
    an append-only ledger.

    The append is deliberately unguarded by a document hash. Two chats
    recording different episodes in the same month are not in conflict — they
    are both appending, and the project lock serialises them. A hash guard
    would manufacture ``PATCH_CONFLICT`` for an operation that has none.
    """
    validated = _validate_episode_draft(workspace_root, project_key, draft)
    now = _episode_now()
    month_rel = f"{EPISODES_FOLDER}/{now:%Y-%m}.md"
    project_dir = contained_project_root(workspace_root, project_key)
    target_file = validate_path_safe(workspace_root, project_key, month_rel)
    episode_id = new_episode_id()
    section = _render_episode(validated, episode_id=episode_id, now=now)
    request_json: JsonObject = {
        "title": validated.title,
        "summary_bytes": len(validated.summary.encode("utf-8")),
        "related_paths_count": len(validated.related_paths),
        "has_related_episode_id": validated.related_episode_id is not None,
    }

    with acquire_project_lock(project_dir, project_key):
        if target_file.is_file():
            return _append_episode_to_month(
                conn,
                workspace_root,
                project_key,
                _EpisodeAppend(
                    month_rel=month_rel,
                    target_file=target_file,
                    section=section,
                    episode_id=episode_id,
                    request_json=request_json,
                ),
            )
        _refuse_archived_episode_month(workspace_root, project_key, month_rel)
        # edit_policy: append is set explicitly. memory/ defaults to free, and
        # free is the wrong default for a ledger whose value is that earlier
        # entries are never rewritten — the same treatment log canvases get.
        header = generate_frontmatter(
            doc_id=new_document_id(),
            project_key=project_key,
            title=f"Episodes {now:%Y-%m}",
            description=(
                f"Episode ledger for {now:%B %Y}: what happened in this project that "
                "month — decisions and the reasoning at the time, incidents, "
                "corrections, and outcomes — appended in order and never rewritten."
            ),
            behavior=FrontmatterBehavior(edit_policy="append"),
        )
        written = _write_new_document_locked(
            conn,
            workspace_root,
            project_key,
            _NewDocumentWrite(
                doc_rel=month_rel,
                full_content=f"{header}\n# Episodes {now:%Y-%m}\n\n{section}",
                operation_type="record_episode",
                request_json=request_json,
            ),
        )
    return EpisodeResult(
        operation_id=written.operation_id,
        snapshot_id=written.snapshot_id,
        path=written.path,
        folder=written.folder,
        document_sha256=written.document_sha256,
        index_error=written.index_error,
        episode_id=episode_id,
        month_file_created=True,
    )
