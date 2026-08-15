"""Generic project file reads: model context and original resources (spec-mcp §5.4).

Two consumers, one resolver:

* ``read_file`` (Tier 1) asks for something a model can *use* — a bounded
  image rendition or bounded text. It never returns the original bytes of a
  binary.
* ``resources/read`` (Tier 2) asks for the *original* — exact bytes, no
  rendition, no truncation.

Both go through :func:`resolve_project_file`, so containment, symlink
refusal, regular-file checks, and the size cap are enforced once. Neither
ever reveals the server-local workspace path: inputs and outputs are
project-relative throughout.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final, Literal

from ferumind.core.errors import (
    FileNotFoundFerumindError,
    FileTooLargeError,
    RenditionTooLargeError,
    ValidationError,
)
from ferumind.core.file_io import read_regular_file_bytes
from ferumind.core.file_uri import build_file_uri
from ferumind.core.files import (
    ContextSupport,
    SidecarMetadata,
    classify_context_support,
    is_markdown_path,
    resolve_mime_type,
    sidecar_for_path,
)
from ferumind.core.paths import (
    PathSafetyError,
    WorkspaceRoot,
    contained_path,
    contained_project_root,
)
from ferumind.core.renditions import (
    DEFAULT_IMAGE_EDGE,
    DEFAULT_IMAGE_QUALITY,
    ImageRendition,
    render_image_bytes,
)
from ferumind.core.types import StrictModel
from ferumind.core.write_limits import MAX_UPLOAD_BYTES

#: Absolute ceiling for resolving any project file, whatever the caller.
#: Equal to the largest file Ferumind will assemble on upload — refusing to
#: serve back something it accepted would be incoherent.
#:
#: This is *not* the effective ``resources/read`` limit. A response also has
#: to survive the caller's transport, which is narrower and configurable
#: (``Config.max_resource_response_bytes``); see
#: ``_assert_resource_is_deliverable`` below. A 20 MB original passes here
#: and is still correctly refused as undeliverable over a 10 MiB tunnel.
MAX_RESOURCE_READ_BYTES: Final = MAX_UPLOAD_BYTES

#: Ceiling for reading a text file into model context. Far below the
#: resource cap: text is decoded and sliced in memory, and anything this
#: large belongs in the resource channel rather than a tool result.
MAX_TEXT_CONTEXT_SOURCE_BYTES: Final = 8 * 1024 * 1024

MIN_TEXT_CHARS: Final = 1
MAX_TEXT_CHARS_LIMIT: Final = 200_000
DEFAULT_MAX_TEXT_CHARS: Final = 50_000


class ResolvedProjectFile(StrictModel):
    """A validated, project-contained regular file."""

    path: str
    absolute: Path
    size_bytes: int
    mime_type: str
    context_support: ContextSupport
    resource_uri: str
    is_markdown: bool


class TextSlice(StrictModel):
    """A bounded window onto a UTF-8 text file."""

    text: str
    offset: int
    returned_chars: int
    total_chars: int
    truncated: bool
    next_offset: int | None = None


class FileContextResult(StrictModel):
    """What ``read_file`` resolved for one project-relative path."""

    file: ResolvedProjectFile
    representation: Literal["image", "text", "resource_only"]
    sha256: str
    sidecar: SidecarMetadata | None = None
    rendition: ImageRendition | None = None
    text: TextSlice | None = None
    reason: str | None = None


class FileResourceContent(StrictModel):
    """The untouched original, for ``resources/read``."""

    path: str
    mime_type: str
    size_bytes: int
    text: str | None = None
    blob: bytes | None = None


def resolve_project_file(
    workspace: WorkspaceRoot,
    project_key: str,
    path: str,
) -> ResolvedProjectFile:
    """Validate and classify one project-relative file path.

    ``contained_path`` does the heavy lifting: absolute paths, ``..``
    traversal, backslashes, control characters, over-long components, and
    symlink components anywhere below the project root are all refused
    there. This adds the checks specific to serving a file — it must exist,
    be a regular file, and fit the resource cap.
    """
    project_root = contained_project_root(workspace, project_key)
    try:
        resolved = contained_path(project_root, path)
    except PathSafetyError as exc:
        # Re-raised as-is: the MCP layer maps PathSafetyError to
        # WORKSPACE_MISMATCH without echoing the absolute path.
        raise exc
    if any(part.startswith(".") for part in Path(path).parts):
        raise FileNotFoundFerumindError(
            f"File not found: {path}",
            details={"path": path, "reason": "dot_prefixed_path"},
        )
    if not resolved.exists():
        raise FileNotFoundFerumindError(f"File not found: {path}", details={"path": path})
    if resolved.is_dir():
        raise ValidationError(
            f"{path} is a directory; use list_files with path_prefix to enumerate it",
            details={"path": path},
        )
    if not resolved.is_file():
        raise ValidationError(
            f"{path} is not a regular file",
            details={"path": path},
        )
    size_bytes = resolved.stat().st_size
    if size_bytes > MAX_RESOURCE_READ_BYTES:
        raise FileTooLargeError(
            f"{path} is larger than Ferumind will serve in one read",
            details={
                "path": path,
                "size_bytes": size_bytes,
                "limit_bytes": MAX_RESOURCE_READ_BYTES,
            },
        )
    mime_type = resolve_mime_type(path)
    return ResolvedProjectFile(
        path=path,
        absolute=resolved,
        size_bytes=size_bytes,
        mime_type=mime_type,
        context_support=classify_context_support(mime_type),
        resource_uri=build_file_uri(project_key, path),
        is_markdown=is_markdown_path(path),
    )


def file_sha256(target: Path) -> str:
    """Return a file's SHA-256, read through the symlink-refusing open."""
    return hashlib.sha256(read_regular_file_bytes(target)).hexdigest()


def _slice_text(content: str, offset: int, max_chars: int) -> TextSlice:
    total = len(content)
    if offset > total:
        raise ValidationError(
            "text_offset is past the end of the file",
            details={"text_offset": offset, "total_chars": total},
        )
    window = content[offset : offset + max_chars]
    end = offset + len(window)
    truncated = end < total
    return TextSlice(
        text=window,
        offset=offset,
        returned_chars=len(window),
        total_chars=total,
        truncated=truncated,
        next_offset=end if truncated else None,
    )


def _read_text_context(
    resolved: ResolvedProjectFile,
    raw: bytes,
    *,
    text_offset: int,
    max_text_chars: int,
) -> tuple[TextSlice | None, str | None]:
    """Decode and slice already-read text, or explain why it stays resource-only.

    The bytes are passed in rather than reopened: the caller read them once
    under the symlink-refusing open, and a second open by name could resolve
    to a different file (S-06).
    """
    if len(raw) > MAX_TEXT_CONTEXT_SOURCE_BYTES:
        raise FileTooLargeError(
            f"{resolved.path} is too large to read as text context",
            details={
                "path": resolved.path,
                "size_bytes": len(raw),
                "limit_bytes": MAX_TEXT_CONTEXT_SOURCE_BYTES,
                "resource_uri": resolved.resource_uri,
            },
        )
    try:
        # Strict: a file whose MIME says text but whose bytes are not UTF-8
        # becomes resource_only rather than lossily-decoded mojibake.
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not_valid_utf8"
    # Slicing happens on the decoded string, so a window boundary can never
    # fall inside a multi-byte character.
    return _slice_text(content, text_offset, max_text_chars), None


def read_file_for_context(
    workspace: WorkspaceRoot,
    project_key: str,
    path: str,
    *,
    max_image_edge: int = DEFAULT_IMAGE_EDGE,
    image_quality: int = DEFAULT_IMAGE_QUALITY,
    text_offset: int = 0,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> FileContextResult:
    """Read one project file into a model-usable representation.

    Images become a bounded rendition, decodable text becomes a bounded
    slice, and everything else is reported as ``resource_only`` with its
    resource URI. The original file is never modified or returned inline.
    """
    if text_offset < 0:
        raise ValidationError("text_offset must not be negative", details={"min": 0})
    if max_text_chars < MIN_TEXT_CHARS or max_text_chars > MAX_TEXT_CHARS_LIMIT:
        raise ValidationError(
            f"max_text_chars must be between {MIN_TEXT_CHARS} and {MAX_TEXT_CHARS_LIMIT}",
            details={"min": MIN_TEXT_CHARS, "max": MAX_TEXT_CHARS_LIMIT},
        )

    resolved = resolve_project_file(workspace, project_key, path)
    project_root = contained_project_root(workspace, project_key)
    sidecar = sidecar_for_path(project_root, path)
    # One read serves the digest and every representation below it, so the
    # hash provably describes the bytes that were rendered or sliced. Two
    # opens of the same name are two different questions (S-06).
    raw = read_regular_file_bytes(resolved.absolute)
    digest = hashlib.sha256(raw).hexdigest()

    if resolved.context_support == "image":
        try:
            rendition = render_image_bytes(
                raw,
                max_edge=max_image_edge,
                quality=image_quality,
            )
        except RenditionTooLargeError:
            # Every encoding would have been larger than the original, so
            # there is nothing to gain by returning one. The original is by
            # definition small here; the resource link already carries it.
            return FileContextResult(
                file=resolved,
                representation="resource_only",
                sha256=digest,
                sidecar=sidecar,
                reason="no_rendition_smaller_than_original",
            )
        return FileContextResult(
            file=resolved,
            representation="image",
            sha256=digest,
            sidecar=sidecar,
            rendition=rendition,
        )

    if resolved.context_support == "text":
        text_slice, reason = _read_text_context(
            resolved,
            raw,
            text_offset=text_offset,
            max_text_chars=max_text_chars,
        )
        if text_slice is None:
            return FileContextResult(
                file=resolved,
                representation="resource_only",
                sha256=digest,
                sidecar=sidecar,
                reason=reason,
            )
        return FileContextResult(
            file=resolved,
            representation="text",
            sha256=digest,
            sidecar=sidecar,
            text=text_slice,
        )

    return FileContextResult(
        file=resolved,
        representation="resource_only",
        sha256=digest,
        sidecar=sidecar,
        reason="no_model_context_rendition_for_mime_type",
    )


#: Base64 inflates a blob by 4/3. A resource whose encoded form would exceed
#: the transport ceiling is refused rather than truncated.
_BASE64_INFLATION_NUMERATOR: Final = 4
_BASE64_INFLATION_DENOMINATOR: Final = 3


def _assert_resource_is_deliverable(size_bytes: int, max_response_bytes: int | None) -> None:
    """Refuse an original that the caller's transport provably cannot carry.

    Checked against the stat size *before* the file is read, so an oversized
    resource costs nothing to reject and never has to be held in memory.

    This is a hard refusal on purpose. ``resources/read`` promises the exact
    original, so silently truncating would break that contract; and emitting
    an oversized reply is worse than useless, because a transport that
    rejects the response body can tear down the connection carrying it and
    leave the server unreachable for every later call.
    """
    if max_response_bytes is None:
        return
    encoded_estimate = (size_bytes * _BASE64_INFLATION_NUMERATOR) // _BASE64_INFLATION_DENOMINATOR
    if encoded_estimate <= max_response_bytes:
        return
    usable = (max_response_bytes * _BASE64_INFLATION_DENOMINATOR) // _BASE64_INFLATION_NUMERATOR
    raise FileTooLargeError(
        "This file is too large to return as a resource. Its base64-encoded "
        f"form would be about {encoded_estimate} bytes, over the "
        f"{max_response_bytes}-byte transport limit. Use read_file for a "
        "bounded rendition or text slice of the same file; that is the "
        "supported way to get this content into context.",
        details={
            "size_bytes": size_bytes,
            "encoded_estimate_bytes": encoded_estimate,
            "max_response_bytes": max_response_bytes,
            "max_original_bytes": usable,
            "recommended_tool": "read_file",
            "recommended_action": (
                "Call read_file with this path for an image rendition or a "
                "bounded text slice. The original stays on disk untouched."
            ),
        },
    )


def read_file_resource(
    workspace: WorkspaceRoot,
    project_key: str,
    path: str,
    *,
    max_response_bytes: int | None = None,
) -> FileResourceContent:
    """Read the untouched original for ``resources/read``.

    Returns text for UTF-8-decodable textual types and raw bytes for
    everything else. Never truncates and never substitutes a rendition: a
    resource represents the original or fails — and when it cannot be
    delivered within *max_response_bytes* it fails loudly, with a pointer to
    ``read_file``.
    """
    resolved = resolve_project_file(workspace, project_key, path)
    _assert_resource_is_deliverable(resolved.size_bytes, max_response_bytes)
    raw = read_regular_file_bytes(resolved.absolute)
    if resolved.context_support == "text":
        try:
            return FileResourceContent(
                path=resolved.path,
                mime_type=resolved.mime_type,
                size_bytes=resolved.size_bytes,
                text=raw.decode("utf-8"),
            )
        except UnicodeDecodeError:
            # Declared textual but not actually UTF-8: serve the exact bytes
            # rather than a lossy decode. The resource is the original.
            pass
    return FileResourceContent(
        path=resolved.path,
        mime_type=resolved.mime_type,
        size_bytes=resolved.size_bytes,
        blob=raw,
    )
