"""Structured error types with stable machine-readable codes.

Every core error carries a stable ``code`` so MCP/CLI layers can surface
it through their structured envelopes without re-classifying messages. All
errors derive from :class:`FerumindError`, which derives from ``ValueError``
to preserve ``except ValueError`` handling at boundaries. ``details`` carries
optional machine-readable recovery data (current hashes, match locations,
limits) so a client can correct a failed call without extra lookup
round-trips.

The code list is spec-mcp §7; there are no session error codes.
"""

from __future__ import annotations

from typing import ClassVar, Final

from ferumind.core.types import JsonObject


class FerumindError(ValueError):
    """Base class for core errors with a stable structured code."""

    code: ClassVar[str] = "VALIDATION_ERROR"

    def __init__(self, message: str, *, details: JsonObject | None = None) -> None:
        super().__init__(message)
        self.details: JsonObject | None = details


class ValidationError(FerumindError):
    """Generic validation failure for a tool input or edit target."""

    code: ClassVar[str] = "VALIDATION_ERROR"


# ── Project scoping ───────────────────────────────────────────────────────────


class ProjectRequiredError(FerumindError):
    """Raised when a scoped call arrives without a project argument."""

    code: ClassVar[str] = "PROJECT_REQUIRED"


class ProjectNotFoundError(FerumindError):
    """Raised when the asserted project key is not in the registry."""

    code: ClassVar[str] = "PROJECT_NOT_FOUND"


class WorkspaceMismatchError(FerumindError):
    """Raised when a target path falls outside the asserted project."""

    code: ClassVar[str] = "WORKSPACE_MISMATCH"


# ── Workspace format ─────────────────────────────────────────────────────────


class FormatUnsupportedError(FerumindError):
    """Raised when the workspace format does not match what this build serves."""

    code: ClassVar[str] = "FORMAT_UNSUPPORTED"


class MigrationPrerequisiteError(FerumindError):
    """Raised when a workspace does not yet satisfy a format's prerequisites.

    Distinct from ``FORMAT_UNSUPPORTED`` on purpose: that says the build and
    the workspace disagree about the format, while this says the migration
    could run but the workspace is not ready for it. It is raised before any
    backup or transformation exists, so nothing has changed and no recovery is
    required — the remedy is to finish preparing and run ``migrate`` again.

    CLI-surface only. ``ferumind migrate`` is the sole entry point to
    migration, so this code never reaches the MCP tool surface.
    """

    code: ClassVar[str] = "MIGRATION_PREREQUISITE_UNMET"


# ── Documents and folders ────────────────────────────────────────────────────


class DocumentNotFoundError(FerumindError):
    code: ClassVar[str] = "DOCUMENT_NOT_FOUND"


class DocumentArchivedError(FerumindError):
    """Raised when a write targets an archived document or archive/ path."""

    code: ClassVar[str] = "DOCUMENT_ARCHIVED"


class UnknownFolderError(FerumindError):
    """Raised when a document path does not start with a role folder."""

    code: ClassVar[str] = "UNKNOWN_FOLDER"


class SkillNotFoundError(FerumindError):
    """Raised when a named Ferumind skill is not installed in the workspace.

    Distinct from ``DOCUMENT_NOT_FOUND``: a skill is workspace-level behaviour
    text under ``system/skills/``, never a project document, so no project
    scope applies and no document path is being resolved.
    """

    code: ClassVar[str] = "SKILL_NOT_FOUND"


class CannotArchiveSpineError(FerumindError):
    code: ClassVar[str] = "CANNOT_ARCHIVE_SPINE"


class PathExistsError(FerumindError):
    """Raised when unarchiving collides with an existing file at the origin."""

    code: ClassVar[str] = "PATH_EXISTS"


class DocumentExistsError(FerumindError):
    """Raised when creating a document at a path that already exists."""

    code: ClassVar[str] = "DOCUMENT_EXISTS"


class FileNotFoundFerumindError(FerumindError):
    """Raised when a project-relative file path resolves to nothing readable.

    Distinct from ``DOCUMENT_NOT_FOUND``: that code is about the managed
    Markdown surface, this one about the generic file surface
    (``list_files``/``read_file``/``resources/read``).
    """

    code: ClassVar[str] = "FILE_NOT_FOUND"


class UnsupportedFileTypeError(FerumindError):
    """Raised when an uploaded file's extension is on the upload blocklist."""

    code: ClassVar[str] = "UNSUPPORTED_FILE_TYPE"


class FileTooLargeError(FerumindError):
    """Raised when a decoded upload payload exceeds the size cap."""

    code: ClassVar[str] = "FILE_TOO_LARGE"


class RenditionTooLargeError(FileTooLargeError):
    """Raised when no rendition fits a file's effective byte ceiling.

    A narrower case than its parent, and the only one ``read_file`` converts
    into a ``resource_only`` result instead of an error: the source decodes
    fine, but every encoding the renderer can produce would be larger than
    the original it is supposed to bound. Subclassing keeps the wire code
    ``FILE_TOO_LARGE`` for callers that do see it raised directly.
    """


class UploadIncompleteError(FerumindError):
    """Raised when finalizing a chunked upload with missing chunks."""

    code: ClassVar[str] = "UPLOAD_INCOMPLETE"


class ContentHashMismatchError(FerumindError):
    """Raised when assembled/decoded upload bytes don't match a caller-supplied hash."""

    code: ClassVar[str] = "CONTENT_HASH_MISMATCH"


# ── Remote fetch (ChatGPT file-reference uploads) ────────────────────────────


class UnsafeUrlError(FerumindError):
    """Raised for a non-HTTPS URL, an unresolvable host, or an SSRF-unsafe address."""

    code: ClassVar[str] = "UNSAFE_URL"


class TooManyRedirectsError(FerumindError):
    """Raised when a remote fetch exceeds the redirect limit."""

    code: ClassVar[str] = "TOO_MANY_REDIRECTS"


class DownloadTimeoutError(FerumindError):
    """Raised when a remote fetch exceeds its connect/read/total timeout."""

    code: ClassVar[str] = "DOWNLOAD_TIMEOUT"


class DownloadFailedError(FerumindError):
    """Raised for a network error or non-2xx response fetching a remote file."""

    code: ClassVar[str] = "DOWNLOAD_FAILED"


# ── Lookup / granular edit targets ───────────────────────────────────────────


class SectionNotFoundError(FerumindError):
    code: ClassVar[str] = "SECTION_NOT_FOUND"


class RangeNotFoundError(FerumindError):
    code: ClassVar[str] = "RANGE_NOT_FOUND"


class RangeTooLargeError(FerumindError):
    code: ClassVar[str] = "RANGE_TOO_LARGE"


class MatchNotFoundError(FerumindError):
    code: ClassVar[str] = "MATCH_NOT_FOUND"


class AmbiguousMatchError(FerumindError):
    code: ClassVar[str] = "AMBIGUOUS_MATCH"


class InvalidRegexError(FerumindError):
    code: ClassVar[str] = "INVALID_REGEX"


# ── Frontmatter ──────────────────────────────────────────────────────────────


class FrontmatterProtectedError(FerumindError):
    code: ClassVar[str] = "FRONTMATTER_PROTECTED"


class FrontmatterRequiredError(FerumindError):
    """Raised when a patch would strip or invalidate required managed frontmatter."""

    code: ClassVar[str] = "FRONTMATTER_REQUIRED"


class FrontmatterInvalidError(FerumindError):
    code: ClassVar[str] = "FRONTMATTER_INVALID"


# ── Patch lifecycle ──────────────────────────────────────────────────────────


class PatchConflictError(FerumindError):
    """Raised when a patch cannot be applied due to a content conflict."""

    code: ClassVar[str] = "PATCH_CONFLICT"


class DocumentHashMismatchError(PatchConflictError):
    code: ClassVar[str] = "DOCUMENT_HASH_MISMATCH"


class TargetHashMismatchError(PatchConflictError):
    code: ClassVar[str] = "TARGET_HASH_MISMATCH"


class PatchExpiredError(FerumindError):
    """Raised when applying a proposal past its 24 h TTL."""

    code: ClassVar[str] = "PATCH_EXPIRED"


class PatchProjectMismatchError(FerumindError):
    """Raised when an operation belongs to a different project than asserted."""

    code: ClassVar[str] = "PATCH_PROJECT_MISMATCH"


class OperationNotFoundError(FerumindError):
    code: ClassVar[str] = "OPERATION_NOT_FOUND"


class InvalidOperationError(FerumindError):
    """Raised when an operation exists but is not usable for the request
    (already applied, discarded, stale, or not a proposal)."""

    code: ClassVar[str] = "INVALID_OPERATION"


# ── Snapshots ────────────────────────────────────────────────────────────────


class SnapshotNotFoundError(FerumindError):
    code: ClassVar[str] = "SNAPSHOT_NOT_FOUND"


# ── Workspace compacts ───────────────────────────────────────────────────────


class CompactNotFoundError(FerumindError):
    code: ClassVar[str] = "COMPACT_NOT_FOUND"


class CompactIntegrityError(FerumindError):
    code: ClassVar[str] = "COMPACT_INTEGRITY_MISMATCH"


class CompactArchivedError(FerumindError):
    code: ClassVar[str] = "COMPACT_ARCHIVED"


ERROR_CODES: Final[tuple[str, ...]] = (
    "INTERNAL_ERROR",
    "VALIDATION_ERROR",
    "PROJECT_REQUIRED",
    "PROJECT_NOT_FOUND",
    "WORKSPACE_MISMATCH",
    "FORMAT_UNSUPPORTED",
    "DOCUMENT_NOT_FOUND",
    "DOCUMENT_ARCHIVED",
    "UNKNOWN_FOLDER",
    "SKILL_NOT_FOUND",
    "CANNOT_ARCHIVE_SPINE",
    "PATH_EXISTS",
    "DOCUMENT_EXISTS",
    "FILE_NOT_FOUND",
    "UNSUPPORTED_FILE_TYPE",
    "FILE_TOO_LARGE",
    "UPLOAD_INCOMPLETE",
    "CONTENT_HASH_MISMATCH",
    "UNSAFE_URL",
    "TOO_MANY_REDIRECTS",
    "DOWNLOAD_TIMEOUT",
    "DOWNLOAD_FAILED",
    "SECTION_NOT_FOUND",
    "RANGE_NOT_FOUND",
    "RANGE_TOO_LARGE",
    "MATCH_NOT_FOUND",
    "AMBIGUOUS_MATCH",
    "INVALID_REGEX",
    "FRONTMATTER_PROTECTED",
    "FRONTMATTER_REQUIRED",
    "FRONTMATTER_INVALID",
    "PATCH_CONFLICT",
    "DOCUMENT_HASH_MISMATCH",
    "TARGET_HASH_MISMATCH",
    "PATCH_EXPIRED",
    "PATCH_PROJECT_MISMATCH",
    "OPERATION_NOT_FOUND",
    "INVALID_OPERATION",
    "SNAPSHOT_NOT_FOUND",
    "COMPACT_NOT_FOUND",
    "COMPACT_INTEGRITY_MISMATCH",
    "COMPACT_ARCHIVED",
)
