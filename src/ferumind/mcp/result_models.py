"""Advertised ``data`` payload shapes for the MCP tool surface.

Each model here is the ``TData`` of a :class:`~ferumind.mcp.models.FerumindResult`
and becomes one tool's ``outputSchema``. See the ``mcp-tool-contracts`` skill
for the rules these follow; the two that shape this file:

* **Reuse a core model when the tool returns it verbatim.** ``get_context``
  serializes :class:`~ferumind.core.context.ProjectContext` unchanged, so that
  class *is* the schema. A model is restated here only when the tool returns a
  projection — which is most of the write family, because those results drop
  server-local fields (``SnapshotInfo.snapshot_dir``) or flatten a nested one.
* **Richness is tiered.** Tools a model plans around get field-level typing;
  bookkeeping confirmations get a handful of scalars. Tool definitions are
  context the caller pays for on every session.

Nothing here may reference ``JsonObject``/``JsonValue`` from
``core.types``: they are defined recursively and their ``$defs`` cannot be
resolved by clients that flatten ``$ref``. Open-ended maps use
:data:`OpaqueMapping` instead.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ferumind.core.compacts import CompactListItem
from ferumind.core.search import SearchResult

#: An open-ended map the schema can only honestly describe as "an object":
#: user-authored YAML frontmatter, upload sidecar metadata. ``Any`` is
#: deliberate — the alternative, ``JsonObject``, is recursive (see module
#: docstring). Values are still validated on construction by the core models
#: that produce them; only the advertised schema is widened.
type OpaqueMapping = dict[str, Any]


class _Payload(BaseModel):
    """Base for every advertised payload: no undeclared fields may leak out.

    Titles are stripped later, at the MCP boundary, by
    :func:`~ferumind.mcp.models.strip_schema_titles`.
    """

    model_config = ConfigDict(extra="forbid")


# ── Reads ────────────────────────────────────────────────────────────────────


class DocumentData(_Payload):
    """``read_document`` — the managed Markdown surface."""

    path: str
    folder: str
    status: str = Field(description="active, gated, frozen, or archived")
    edit_policy: str = Field(description="free, append, propose-first, or ask-human")
    title: str
    description: str = Field(description="What this document is for, in one or two sentences")
    content: str = Field(description="Full document text, frontmatter included")
    frontmatter: OpaqueMapping
    document_sha256: str = Field(description="Guard value for a later hash-checked edit")


class SearchResultsData(_Payload):
    """``search_project`` — bm25-ranked section hits with line ranges."""

    query: str
    count: int
    results: list[SearchResult]


#: Alias kept for callers/tests that still name the per-hit model ``SearchHit``.
SearchHit = SearchResult


class TreeEntry(_Payload):
    path: str
    title: str
    folder: str
    status: str
    edit_policy: str
    size: int


class TreeListingData(_Payload):
    """``list_tree`` — paths and metadata only, not paginated."""

    count: int
    tree: list[TreeEntry]


class PendingPatch(_Payload):
    operation_id: str
    operation_type: str
    path: str | None
    created_at: str
    expires_at: str | None
    base_sha256: str | None


class PendingPatchesData(_Payload):
    """``list_pending_patches`` — expired proposals are swept before listing."""

    count: int
    pending: list[PendingPatch]


class OperationEntry(_Payload):
    operation_id: str
    operation_type: str
    path: str | None
    source: str = Field(description="Where the change came from, e.g. out-of-band")
    state: str
    created_at: str
    snapshot_id: str | None


class OperationLogData(_Payload):
    """``operation_log`` — metadata only; never document content."""

    count: int
    operations: list[OperationEntry]


class SnapshotEntry(_Payload):
    id: str
    project_key: str
    target_path: str | None
    reason: str
    created_at: str


class SnapshotListingData(_Payload):
    """``list_snapshots`` — metadata only; use read_snapshot for content."""

    count: int
    snapshots: list[SnapshotEntry]


class ProjectSummary(_Payload):
    key: str
    title: str
    status: str


class ProjectListingData(_Payload):
    """``list_projects`` — workspace level, no project scope."""

    count: int
    projects: list[ProjectSummary]


class SkillData(_Payload):
    """``read_skill`` — workspace level, no project scope.

    A Ferumind skill is workspace-wide behaviour under ``system/skills/``, not
    a project document, so no project argument applies and no
    ``document_sha256`` is offered: skills are read, never edited by an agent.
    """

    name: str
    description: str
    path: str
    content_markdown: str


# ── Files (spec-mcp §5.4) ────────────────────────────────────────────────────


class FileEntry(_Payload):
    """Restated rather than reusing ``core.files.ProjectFileEntry``: that model
    reaches ``JsonObject`` through its sidecar metadata, which is recursive.
    """

    path: str
    filename: str
    extension: str
    mime_type: str
    size_bytes: int
    modified_at: str
    resource_uri: str = Field(description="ferumind:// URI for the untouched original")
    context_support: str = Field(description="image, text, or resource_only")
    is_markdown: bool
    is_upload_sidecar: bool
    sidecar: FileSidecar | None = None


class FileListingData(_Payload):
    """``list_files`` — generic discovery; Markdown and sidecars excluded."""

    count: int
    files: list[FileEntry]
    has_more: bool
    next_cursor: str | None
    scanned_count: int


class FileOriginal(_Payload):
    mime_type: str | None
    size_bytes: int
    sha256: str
    width: int | None = None
    height: int | None = None


class FileRendition(_Payload):
    mime_type: str
    size_bytes: int
    width: int
    height: int
    resized: bool
    size_limited: bool
    size_limit_bytes: int
    encode_quality: int | None = None


class FileTextSlice(_Payload):
    offset: int
    returned_chars: int
    total_chars: int
    truncated: bool
    next_offset: int | None


class FileSidecar(_Payload):
    path: str
    metadata: OpaqueMapping


class FileContextData(_Payload):
    """``read_file`` — one file as model context.

    The representation decides which optional branch is present:
    ``image`` fills ``rendition``, ``text`` fills ``text``, and
    ``resource_only`` fills neither, meaning the model has **not** seen the
    contents. Bytes never appear here — an image travels only as a genuine
    ``ImageContent`` block alongside this envelope.
    """

    path: str
    resource_uri: str
    representation: str = Field(description="image, text, or resource_only")
    context_support: str
    is_markdown: bool
    original: FileOriginal
    rendition: FileRendition | None = None
    text: FileTextSlice | None = None
    sidecar: FileSidecar | None = None
    reason: str | None = Field(default=None, description="Why a fuller representation was refused")
    recommended_tool: str | None = Field(
        default=None, description="Set to read_document when the target is Markdown"
    )


# ── Proposals ────────────────────────────────────────────────────────────────


class ProposalPolicy(_Payload):
    """The target's declared policy. The server informs; agents honor."""

    edit_policy: str
    status: str
    policy_note: str | None


class NextRequiredArguments(_Payload):
    operation_id: str
    project: str


class ProposalData(_Payload):
    """Shared by all eight ``propose_*`` tools.

    A proposal is **not a saved edit**. ``document_mutated`` is false and
    ``requires_apply`` is true until ``apply_patch`` runs against this
    ``operation_id``.
    """

    operation_id: str
    path: str
    folder: str
    proposal_kind: str
    document_before_sha256: str | None
    target_before_sha256: str | None
    after_sha256: str
    diff: str
    expires_at: str = Field(description="Proposals expire after 24h with PATCH_EXPIRED")
    deduped: bool
    policy: ProposalPolicy
    operation_status: Literal["proposed"]
    document_mutated: Literal[False]
    requires_apply: Literal[True]
    next_required_tool: Literal["apply_patch"]
    completion_state: Literal["pending_apply"]
    user_visible_state: Literal["not_saved"]
    recommended_action: str
    next_required_arguments: NextRequiredArguments


class DiscardPatchData(_Payload):
    """``discard_patch`` — withdrawing a proposal never touches the document."""

    operation_id: str
    path: str
    state: str
    document_mutated: Literal[False]


# ── Writes ───────────────────────────────────────────────────────────────────


class WriteData(_Payload):
    """``create_document`` and ``capture_note`` — the document was saved."""

    operation_id: str
    snapshot_id: str | None
    path: str
    folder: str | None
    document_sha256: str | None
    index_error: str | None
    document_mutated: Literal[True]


class EpisodeData(_Payload):
    """``record_episode`` — the episode was appended and the file was saved.

    ``month_file_created`` distinguishes the first episode of a month (the
    call that created ``memory/episodes/YYYY-MM.md``) from every later append
    into it.
    """

    operation_id: str
    snapshot_id: str | None
    path: str
    folder: str | None
    document_sha256: str | None
    index_error: str | None
    episode_id: str
    month_file_created: bool
    document_mutated: Literal[True]


class ApplyPatchData(_Payload):
    """``apply_patch`` — the only result that means a file was written.

    Chain ``document_sha256`` into the next edit's
    ``expected_document_sha256`` instead of re-reading the document.
    """

    operation_id: str
    snapshot_id: str | None
    path: str
    folder: str | None
    document_sha256: str | None
    index_error: str | None
    diff: str
    operation_status: Literal["applied"]
    document_mutated: Literal[True]
    requires_apply: Literal[False]
    completion_state: Literal["saved"]
    user_visible_state: Literal["saved"]
    proposal_operation_id: str
    applied_operation_id: str
    recommended_action: str


class RestoreSnapshotData(_Payload):
    """``restore_snapshot`` — the restore is itself snapshot-protected."""

    operation_id: str
    snapshot_id: str | None
    path: str
    folder: str | None
    document_sha256: str | None
    index_error: str | None
    restored_from_snapshot_id: str | None
    rollback_snapshot_id: str | None = Field(
        description="Snapshot of the pre-restore state, so a restore is reversible"
    )
    document_mutated: Literal[True]


class ArchiveData(_Payload):
    """``archive_document`` / ``unarchive_document`` — never a hard delete."""

    operation_id: str
    snapshot_id: str
    path: str
    archived_path: str
    document_sha256: str
    document_mutated: Literal[True]


class UploadData(_Payload):
    """``upload_library_file`` / ``finalize_library_file_upload``.

    ``sha256`` and ``size_bytes`` describe the **stored** bytes: images are
    downscaled and re-encoded on the way in.
    """

    operation_id: str
    snapshot_id: str
    path: str
    metadata_path: str
    folder: str | None
    sha256: str
    size_bytes: int
    mime_type: str | None
    document_mutated: Literal[True]


class UploadSessionData(_Payload):
    """``start_library_file_upload`` — nothing is stored until finalize."""

    upload_id: str
    expires_at: str
    chunk_size_hint: int
    next_required_tool: Literal["append_upload_chunk"]


class ChunkAppendData(_Payload):
    """``append_upload_chunk`` — progress only."""

    upload_id: str
    received_chunks: int
    total_chunks: int | None
    received_bytes: int
    complete: bool


class DiscardUploadData(_Payload):
    """``discard_upload`` — abandons a session; nothing was written."""

    upload_id: str
    path: str | None
    state: str


class CreateProjectData(_Payload):
    """``create_project`` — registry entry plus the seeded contract files."""

    key: str
    title: str
    path: str
    operation_id: str
    snapshot_id: str
    seeded: list[str]


class RebuildIndexData(_Payload):
    """``rebuild_index`` — touches the SQLite index, never user Markdown."""

    projects: list[str]
    documents_indexed: int
    documents_removed: int
    errors: int
    error_messages: list[str]


# ── Compacts ─────────────────────────────────────────────────────────────────


class CompactInstructionsData(_Payload):
    """``get_compact_instructions`` — the procedure, not a stored compact."""

    instructions: str


class CompactListingData(_Payload):
    """``list_compacts`` — workspace level; compacts are not project memory.

    Reuses the core item model: ``list_compacts`` serializes it unchanged.
    """

    compacts: list[CompactListItem]


SearchResultsData.model_rebuild()
TreeListingData.model_rebuild()
PendingPatchesData.model_rebuild()
OperationLogData.model_rebuild()
SnapshotListingData.model_rebuild()
ProjectListingData.model_rebuild()
FileListingData.model_rebuild()
CompactListingData.model_rebuild()
