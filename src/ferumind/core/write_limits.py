"""Bounds every write path is measured against, and the upload sidecar mapping.

A leaf module on purpose. The write domain is split across
``core/patch_writes.py`` and its siblings, but three *read*-side modules need
these same numbers: ``core/file_reads.py`` (``MAX_UPLOAD_BYTES``),
``core/files.py`` (``MAX_UPLOAD_METADATA_BYTES``, ``upload_metadata_path``)
and ``core/image_maintenance.py`` (``upload_metadata_path``). Pointing them at
a write module would make the read side depend on the write side for the sake
of a handful of integers. Nothing here imports from ``ferumind.core``, so it
can never close a cycle.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Final

#: Extensions a managed Markdown write may target.
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

MAX_EPISODE_TITLE_CHARS: Final = 200
MAX_EPISODE_SUMMARY_CHARS: Final = 8_000
MAX_EPISODE_RELATED_PATHS: Final = 20

EPISODES_FOLDER: Final = "memory/episodes"

#: Bounds worst-case per-call work: MAX_CHATGPT_FILES_PER_CALL files, each
#: up to MAX_UPLOAD_BYTES, fetched sequentially in one tool call. Aggregate
#: byte and wall-clock ceilings prevent a valid batch from multiplying the
#: per-file limit into an availability attack.
MAX_CHATGPT_FILES_PER_CALL: Final = 20
MAX_CHATGPT_BATCH_BYTES: Final = 64 * 1024 * 1024
MAX_CHATGPT_BATCH_SECONDS: Final = 60.0


def upload_metadata_path(content_path: str) -> str:
    """Return the sidecar path for an uploaded file (extension replaced, not appended).

    Public because file discovery (``core.files``) has to run this mapping in
    reverse to tell a Ferumind-generated sidecar apart from a user-authored
    ``.json`` document that merely shares a stem.
    """
    path = PurePosixPath(content_path)
    if path.suffix.lower() == ".json":
        return f"{content_path}.metadata.json"
    return path.with_suffix(".json").as_posix()
