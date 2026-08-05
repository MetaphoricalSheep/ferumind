from pathlib import Path, PurePosixPath
from typing import Final, NewType

RepoRoot = NewType("RepoRoot", Path)
WorkspaceRoot = NewType("WorkspaceRoot", Path)
ProjectKey = NewType("ProjectKey", str)

MAX_RELATIVE_PATH_BYTES: Final = 4096
MAX_PATH_COMPONENT_BYTES: Final = 255


class PathSafetyError(ValueError):
    """Raised when a path operation violates safety constraints."""


def is_under_root(candidate: Path, root: Path) -> bool:
    """Return True if *candidate* (after symlink resolution) is contained in *root*.

    Both paths are fully resolved before the check.  Equality with *root* is allowed.
    Sibling-prefix paths such as ``/tmp/root-evil`` are correctly rejected.
    """
    resolved_candidate = candidate.resolve()
    resolved_root = root.resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def resolve_repo_root(path: Path | None = None) -> RepoRoot:
    """Walk upward from *path* to find the repository root (where ``pyproject.toml`` lives)."""
    start = path.resolve() if path else Path.cwd().resolve()
    for candidate in [start, *start.parents]:
        if (candidate / "pyproject.toml").is_file():
            return RepoRoot(candidate)
    msg = f"Could not find repository root (pyproject.toml) from {start}"
    raise PathSafetyError(msg)


def resolve_workspace_root(
    repo: RepoRoot,
    subpath: str | None = None,
) -> WorkspaceRoot:
    """Resolve the workspace root, defaulting to ``<repo>/workspace``."""
    base = repo / (subpath or "workspace")
    resolved = base.resolve()
    if not is_under_root(resolved, repo):
        msg = f"Workspace path {resolved} escapes repository root {repo}"
        raise PathSafetyError(msg)
    return WorkspaceRoot(resolved)


def contained_path(root: Path, user_path: str) -> Path:
    """Resolve a relative path under *root*, refusing traversal and symlinks.

    The configured root itself may be a symlink (it is an operator-selected
    trust boundary), but paths below it may not contain symlink components.
    Rejecting in-root symlinks as well as escaping ones preserves the
    ``folder = role`` invariant: a lexical ``canvases/x.md`` must never
    resolve to ``archive/x.md`` and inherit the wrong policy.
    """
    encoded_path = user_path.encode("utf-8")
    if (
        not encoded_path
        or len(encoded_path) > MAX_RELATIVE_PATH_BYTES
        or "\\" in user_path
        or any(ord(character) < 32 or ord(character) == 127 for character in user_path)
    ):
        raise PathSafetyError(
            "Path is empty, contains unsafe characters, or exceeds the safe length limit"
        )
    supplied = Path(user_path)
    if supplied.is_absolute():
        msg = f"Path {user_path} escapes root {root}"
        raise PathSafetyError(msg)
    if user_path != "." and PurePosixPath(user_path).as_posix() != user_path:
        raise PathSafetyError("Path must be a canonical relative POSIX path")

    for part in supplied.parts:
        if len(part.encode("utf-8")) > MAX_PATH_COMPONENT_BYTES:
            raise PathSafetyError("Path component exceeds the filesystem-safe length limit")

    resolved_root = root.resolve()
    current = resolved_root
    for part in supplied.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise PathSafetyError("Parent-directory traversal is not allowed")
        current = current / part
        try:
            current.relative_to(resolved_root)
        except ValueError as exc:
            msg = f"Path {user_path} escapes root {root}"
            raise PathSafetyError(msg) from exc
        if current.is_symlink():
            msg = f"Symlinks are not allowed below root {root}: {current}"
            raise PathSafetyError(msg)

    candidate = (resolved_root / supplied).resolve()
    if not is_under_root(candidate, resolved_root):
        msg = f"Path {user_path} escapes root {root}"
        raise PathSafetyError(msg)
    return candidate


def contained_project_root(workspace: Path, project_key: str) -> Path:
    """Resolve one project root without treating a project symlink as trusted."""
    projects_root = contained_path(workspace, "projects")
    return contained_path(projects_root, project_key)


def assert_no_symlink_escape(path: Path, root: Path) -> None:
    """Verify that *path* (after symlink resolution) stays under *root*."""
    if not is_under_root(path, root):
        msg = f"Symlink-resolved path {path.resolve()} escapes root {root.resolve()}"
        raise PathSafetyError(msg)
