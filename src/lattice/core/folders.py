"""Folder = role: the v2 project layout (product/spec-mcp.md §2, 00 D2).

A document's role derives from its first path segment; there is no ``role:``
frontmatter key. Role folders nest arbitrarily below the first segment.
``spine.md`` at the project root is the spine. ``archive/`` mirrors the
folder a document came from.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Final, Literal

from lattice.core.errors import UnknownFolderError

type Folder = Literal["spine", "rules", "canvases", "memory", "library", "inbox", "archive"]

SPINE_FILENAME: Final = "spine.md"

ROLE_FOLDERS: Final[tuple[str, ...]] = (
    "rules",
    "canvases",
    "memory",
    "library",
    "inbox",
    "archive",
)

#: Folders a new document may be created in (archive is entered only via
#: archive_document; the spine ships with the project).
CREATABLE_FOLDERS: Final[tuple[str, ...]] = (
    "rules",
    "canvases",
    "memory",
    "library",
    "inbox",
)

#: Project skeleton created by create_project.
PROJECT_DIRECTORIES: Final[tuple[str, ...]] = ROLE_FOLDERS

#: edit_policy defaults by folder when the frontmatter key is absent
#: (spec-mcp §3). A log canvas is an ordinary canvas whose author sets
#: ``edit_policy: append`` explicitly.
DEFAULT_EDIT_POLICY: Final[dict[str, str]] = {
    "spine": "propose-first",
    "rules": "ask-human",
    "canvases": "free",
    "memory": "free",
    "library": "propose-first",
    "inbox": "free",
    "archive": "propose-first",
}


def folder_of(path: str) -> Folder:
    """Derive the role folder from a project-relative document path.

    Raises :class:`UnknownFolderError` for paths outside the layout.
    """
    parts = PurePosixPath(path).parts
    if not parts:
        raise UnknownFolderError("Empty document path has no role folder")
    if len(parts) == 1:
        if parts[0] == SPINE_FILENAME:
            return "spine"
        msg = (
            f"{path!r} is not inside a role folder; project-root documents "
            f"other than {SPINE_FILENAME} are not part of the layout"
        )
        raise UnknownFolderError(msg, details={"allowed_folders": list(ROLE_FOLDERS)})
    first = parts[0]
    folder_literals: dict[str, Folder] = {
        "rules": "rules",
        "canvases": "canvases",
        "memory": "memory",
        "library": "library",
        "inbox": "inbox",
        "archive": "archive",
    }
    result = folder_literals.get(first)
    if result is not None:
        return result
    msg = f"Unknown role folder {first!r} in path {path!r}"
    raise UnknownFolderError(msg, details={"allowed_folders": list(ROLE_FOLDERS)})


def is_archived_path(path: str) -> bool:
    """Return whether *path* lives under ``archive/``."""
    parts = PurePosixPath(path).parts
    return bool(parts) and parts[0] == "archive"


def default_edit_policy(folder: Folder) -> str:
    """Return the folder-default edit policy."""
    return DEFAULT_EDIT_POLICY[folder]


def archive_path_for(path: str) -> str:
    """Return the mirror path under ``archive/`` for a document path."""
    return f"archive/{path}"


def origin_path_for(archived_path: str) -> str:
    """Return the mirror-origin path for a document under ``archive/``."""
    parts = PurePosixPath(archived_path).parts
    if len(parts) < 2 or parts[0] != "archive":
        msg = f"{archived_path!r} is not an archived document path"
        raise UnknownFolderError(msg)
    return str(PurePosixPath(*parts[1:]))
