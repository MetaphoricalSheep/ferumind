"""Core domain logic — shared by all interface layers."""

from lattice.core.paths import (
    PathSafetyError,
    ProjectKey,
    RepoRoot,
    WorkspaceRoot,
    assert_no_symlink_escape,
    contained_path,
    is_under_root,
    resolve_repo_root,
    resolve_workspace_root,
)
from lattice.core.security import (
    allowed_extension,
    assert_not_symlink,
    validate_path_safety,
)

__all__ = [
    "PathSafetyError",
    "ProjectKey",
    "RepoRoot",
    "WorkspaceRoot",
    "allowed_extension",
    "assert_no_symlink_escape",
    "assert_not_symlink",
    "contained_path",
    "is_under_root",
    "resolve_repo_root",
    "resolve_workspace_root",
    "validate_path_safety",
]
