"""Generic project file discovery and MIME classification (spec-mcp §5.4).

Ferumind manages Markdown documents; a project may also contain arbitrary
files an agent put there — photographs, PDFs, spreadsheets, exports. Those
files are invisible to the Markdown tools (``list_tree``/``search_project``
index ``*.md`` only), so this module provides the generic discovery half of
the file surface: walk the project, classify what is there, and hand back
project-relative paths plus canonical resource URIs.

There is no prescribed folder for non-Markdown files. A file is discovered
wherever it lives inside the project; ``library/`` is where *uploads* land,
not where files must be. Nothing here interprets a file's meaning — folder
and extension classify transport, never semantics.

Discovery is deliberately not an index: it walks the project on each call.
That keeps out-of-band files (copied in by the human, synced by a tool)
discoverable with no reconciliation step, at the cost of scaling with
project size rather than result size.
"""

from __future__ import annotations

import json
import mimetypes
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final, Literal, cast

from ferumind.core.errors import ValidationError
from ferumind.core.file_uri import build_file_uri, decode_relative_path, encode_relative_path
from ferumind.core.paths import contained_path
from ferumind.core.types import JsonObject, JsonValue, StrictModel
from ferumind.core.write_limits import MAX_UPLOAD_METADATA_BYTES, upload_metadata_path

#: How a file can reach model context through ``read_file``.
#:
#: ``image``          — a bounded raster rendition is generated.
#: ``text``           — the file decodes as UTF-8 and is served as text.
#: ``resource_only``  — no model-usable rendition exists; the untouched
#:                      original is reachable through its resource URI.
type ContextSupport = Literal["image", "text", "resource_only"]

#: Raster formats Pillow decodes safely and re-encodes to a context
#: rendition. GIF is deliberately absent: presenting the first frame of an
#: animation as "the image" misrepresents the file, so GIF stays
#: ``resource_only``. SVG is absent because rasterizing it would mean
#: executing untrusted markup in a rendering stack Ferumind does not ship.
RENDERABLE_IMAGE_MIME_TYPES: Final[frozenset[str]] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)

#: Non-``text/*`` MIME types that are nonetheless UTF-8 text. Kept as a
#: closed allowlist: an unrecognized type is never speculatively decoded.
TEXTUAL_MIME_TYPES: Final[frozenset[str]] = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "application/javascript",
        "application/x-sh",
        "application/sql",
        "image/svg+xml",
    }
)

#: Extension → MIME overrides applied before ``mimetypes.guess_type``.
#: ``mimetypes`` consults system files that differ between machines and
#: containers; pinning the types Ferumind reasons about keeps classification
#: (and therefore test results) identical everywhere.
_EXTENSION_MIME_TYPES: Final[dict[str, str]] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".yml": "application/yaml",
    ".yaml": "application/yaml",
    ".toml": "application/toml",
    ".xml": "application/xml",
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".py": "text/x-python",
    ".sql": "application/sql",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}

DEFAULT_MIME_TYPE: Final = "application/octet-stream"
MARKDOWN_MIME_TYPE: Final = "text/markdown"

#: Server-stamped keys every Ferumind upload sidecar carries
#: (``upload_writes._write_uploaded_file``). A ``.json`` file is treated as a
#: sidecar only when it carries all of them *and* a matching content file
#: exists, so a user-authored ``.json`` is never hidden from discovery.
UPLOAD_SIDECAR_STAMPED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "original_filename",
        "uploaded_at",
        "uploaded_by_tool",
        "sha256",
        "size_bytes",
    }
)

#: Sidecars are bounded on write; bound them again on read so a file edited
#: out of band cannot turn a listing into an unbounded parse.
MAX_SIDECAR_READ_BYTES: Final = MAX_UPLOAD_METADATA_BYTES

#: Bounds on sidecar metadata echoed into a listing entry. Discovery
#: returns a hint, not the document: ``read_file`` carries the fuller view.
MAX_LISTED_SIDECAR_KEYS: Final = 10
MAX_LISTED_SIDECAR_VALUE_CHARS: Final = 120

MIN_LIST_LIMIT: Final = 1
MAX_LIST_LIMIT: Final = 500
DEFAULT_LIST_LIMIT: Final = 100

#: Directory names never walked. Everything dot-prefixed is skipped, which
#: covers ``.ferumind/`` (snapshots, upload staging, the database) without
#: enumerating internals here.
_SKIP_DIR_NAMES: Final[frozenset[str]] = frozenset({".ferumind"})


class SidecarMetadata(StrictModel):
    """A recognized Ferumind upload sidecar, bounded for transport."""

    path: str
    metadata: JsonObject


class ProjectFileEntry(StrictModel):
    """One discovered project file. Paths are always project-relative."""

    path: str
    filename: str
    extension: str
    mime_type: str
    size_bytes: int
    modified_at: str
    resource_uri: str
    context_support: ContextSupport
    is_markdown: bool
    is_upload_sidecar: bool
    sidecar: SidecarMetadata | None = None


class FileListing(StrictModel):
    """A deterministic page of discovered files."""

    files: list[ProjectFileEntry]
    count: int
    has_more: bool
    next_cursor: str | None = None
    scanned_count: int


def resolve_mime_type(path: str) -> str:
    """Classify a project-relative path's MIME type conservatively."""
    suffix = PurePosixPath(path).suffix.lower()
    override = _EXTENSION_MIME_TYPES.get(suffix)
    if override is not None:
        return override
    guessed, _encoding = mimetypes.guess_type(path)
    return guessed or DEFAULT_MIME_TYPE


def classify_context_support(mime_type: str) -> ContextSupport:
    """Return how ``read_file`` can present this MIME type to a model."""
    if mime_type in RENDERABLE_IMAGE_MIME_TYPES:
        return "image"
    if mime_type.startswith("text/") or mime_type in TEXTUAL_MIME_TYPES:
        return "text"
    return "resource_only"


def is_markdown_path(path: str) -> bool:
    """Return whether *path* is a managed Markdown document by extension."""
    return PurePosixPath(path).suffix.lower() == ".md"


def _has_sidecar_content_file(project_root: Path, sidecar_rel: str) -> bool:
    """Return whether some content file on disk would produce *sidecar_rel*.

    Inverts :func:`ferumind.core.write_limits.upload_metadata_path`. A
    ``.metadata.json`` name belongs to exactly one content path; the
    extension-replaced form (``photo.json``) belongs to any sibling sharing
    the stem, so the directory is consulted rather than guessed.
    """
    path = PurePosixPath(sidecar_rel)
    if path.suffix.lower() != ".json":
        return False
    name = path.name
    if name.lower().endswith(".metadata.json"):
        base = name[: -len(".metadata.json")]
        if not base or not base.lower().endswith(".json"):
            return False
        content = project_root / path.with_name(base)
        return content.is_file() and not content.is_symlink()

    parent = project_root / path.parent
    stem = path.stem
    try:
        siblings = list(parent.iterdir())
    except OSError:
        return False
    return any(
        sibling.name != name
        and PurePosixPath(sibling.name).stem == stem
        and sibling.is_file()
        and not sibling.is_symlink()
        for sibling in siblings
    )


def is_recognized_upload_sidecar(project_root: Path, relative: str) -> JsonObject | None:
    """Return bounded sidecar metadata when *relative* is a Ferumind sidecar.

    Both halves must hold: a content file that would generate this exact
    sidecar path, and the server-stamped keys inside. A user-authored
    ``.json`` that merely shares a stem fails the second check and stays a
    first-class listed file.
    """
    if not _has_sidecar_content_file(project_root, relative):
        return None
    return read_upload_sidecar(project_root / relative)


def _bounded_sidecar_scalars(source: dict[str, object]) -> JsonObject:
    """Keep only bounded top-level scalars from parsed sidecar JSON."""
    bounded: JsonObject = {}
    for key in sorted(source):
        if len(bounded) >= MAX_LISTED_SIDECAR_KEYS:
            break
        value = source[key]
        if isinstance(value, bool | int | float):
            bounded[key] = value
        elif isinstance(value, str):
            bounded[key] = value[:MAX_LISTED_SIDECAR_VALUE_CHARS]
    return bounded


def read_upload_sidecar(sidecar_file: Path) -> JsonObject | None:
    """Parse a candidate sidecar, returning ``None`` when it is not one.

    Bounded and total: an oversized, unreadable, malformed, or
    wrong-shaped file yields ``None`` rather than raising, so one bad
    sidecar can never break a listing.
    """
    try:
        if sidecar_file.stat().st_size > MAX_SIDECAR_READ_BYTES:
            return None
        raw = sidecar_file.read_bytes()
    except OSError:
        return None
    try:
        parsed: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    typed = cast("dict[str, object]", parsed)
    if not UPLOAD_SIDECAR_STAMPED_KEYS.issubset(typed.keys()):
        return None
    return _bounded_sidecar_scalars(typed)


def _iter_project_files(project_root: Path) -> Iterator[tuple[str, os.stat_result]]:
    """Yield ``(project-relative posix path, stat)`` for every regular file.

    Symlinked files and directories are skipped outright rather than
    resolved: ``contained_path`` refuses to serve them later anyway, so
    listing them would advertise paths that cannot be read.
    """
    stack: list[tuple[Path, str]] = [(project_root, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name in _SKIP_DIR_NAMES:
                continue
            relative = f"{prefix}{name}"
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), f"{relative}/"))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                yield relative, entry.stat(follow_symlinks=False)
            except OSError:
                continue


def _modified_at(stat: os.stat_result) -> str:
    return datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat()


def _matches_query(entry: ProjectFileEntry, needle: str) -> bool:
    """Case-insensitive substring match across discovery-visible fields."""
    haystacks = [
        entry.path,
        entry.filename,
        entry.extension,
        entry.mime_type,
    ]
    if entry.sidecar is not None:
        haystacks.extend(str(value) for value in entry.sidecar.metadata.values())
    return any(needle in haystack.lower() for haystack in haystacks)


def _decode_cursor(cursor: str) -> str:
    """Decode a listing cursor into the path it points just past."""
    try:
        return decode_relative_path(cursor)
    except ValidationError as exc:
        raise ValidationError("cursor is not a valid listing cursor") from exc


def list_project_files(
    project_root: Path,
    project_key: str,
    *,
    path_prefix: str | None = None,
    query: str | None = None,
    mime_type: str | None = None,
    extension: str | None = None,
    include_markdown: bool = False,
    include_sidecars: bool = False,
    limit: int = DEFAULT_LIST_LIMIT,
    cursor: str | None = None,
) -> FileListing:
    """Discover files under one project, filtered and paginated deterministically.

    ``path_prefix`` is validated through :func:`contained_path` so a prefix
    can never be used to walk out of the project.
    """
    if limit < MIN_LIST_LIMIT or limit > MAX_LIST_LIMIT:
        raise ValidationError(
            f"limit must be between {MIN_LIST_LIMIT} and {MAX_LIST_LIMIT}",
            details={"min": MIN_LIST_LIMIT, "max": MAX_LIST_LIMIT},
        )

    prefix = ""
    if path_prefix:
        # A trailing slash is the natural way to write a directory prefix,
        # but it is not a canonical relative path, so strip it before the
        # containment check rather than rejecting the friendlier spelling.
        cleaned = path_prefix.strip("/")
        if cleaned:
            # Containment first: a rejected prefix must fail as a path-safety
            # error, not silently match nothing.
            contained_path(project_root, cleaned)
            # Compared with a trailing slash so this is a directory test, not
            # a name test: "library" must not select a sibling directory
            # named "library-archive". This filters paths the contained walk
            # already produced — ``contained_path`` above is what enforces
            # containment.
            prefix = f"{cleaned}/"

    needle = query.strip().lower() if query else None
    wanted_mime = mime_type.strip().lower() if mime_type else None
    wanted_ext = extension.strip().lower() if extension else None
    if wanted_ext and not wanted_ext.startswith("."):
        wanted_ext = f".{wanted_ext}"

    after_path = _decode_cursor(cursor) if cursor else None

    matched: list[ProjectFileEntry] = []
    scanned = 0
    for relative, stat in _iter_project_files(project_root):
        scanned += 1
        if prefix and not relative.startswith(prefix):
            continue
        resolved_mime = resolve_mime_type(relative)
        markdown = is_markdown_path(relative)
        if markdown and not include_markdown:
            continue
        is_sidecar = is_recognized_upload_sidecar(project_root, relative) is not None
        if is_sidecar and not include_sidecars:
            continue
        if wanted_mime and resolved_mime.lower() != wanted_mime:
            continue
        suffix = PurePosixPath(relative).suffix.lower()
        if wanted_ext and suffix != wanted_ext:
            continue

        entry = ProjectFileEntry(
            path=relative,
            filename=PurePosixPath(relative).name,
            extension=suffix,
            mime_type=resolved_mime,
            size_bytes=stat.st_size,
            modified_at=_modified_at(stat),
            resource_uri=build_file_uri(project_key, relative),
            context_support=classify_context_support(resolved_mime),
            is_markdown=markdown,
            is_upload_sidecar=is_sidecar,
            sidecar=None if is_sidecar else sidecar_for_path(project_root, relative),
        )
        if needle and not _matches_query(entry, needle):
            continue
        matched.append(entry)

    matched.sort(key=lambda item: item.path)
    if after_path is not None:
        matched = [item for item in matched if item.path > after_path]

    page = matched[:limit]
    has_more = len(matched) > limit
    next_cursor = encode_relative_path(page[-1].path) if has_more and page else None
    return FileListing(
        files=page,
        count=len(page),
        has_more=has_more,
        next_cursor=next_cursor,
        scanned_count=scanned,
    )


def sidecar_for_path(project_root: Path, path: str) -> SidecarMetadata | None:
    """Return the recognized upload sidecar for one content path, if any."""
    if is_markdown_path(path):
        return None
    sidecar_rel = upload_metadata_path(path)
    sidecar_file = project_root / sidecar_rel
    if not sidecar_file.is_file() or sidecar_file.is_symlink():
        return None
    payload = read_upload_sidecar(sidecar_file)
    if payload is None:
        return None
    return SidecarMetadata(path=sidecar_rel, metadata=payload)


def dump_entry(entry: ProjectFileEntry) -> JsonObject:
    """Serialize a listing entry for a tool envelope."""
    data: dict[str, JsonValue] = entry.model_dump(mode="json", exclude_none=True)
    return data
