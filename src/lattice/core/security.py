from collections.abc import Sequence
from pathlib import Path

from lattice.core.paths import PathSafetyError, contained_path


def validate_path_safety(path: Path, allowed_roots: Sequence[Path]) -> Path:
    """Canonicalize *path* under one root, refusing descendant symlinks."""
    absolute_path = path.absolute()
    for root in allowed_roots:
        absolute_root = root.absolute()
        try:
            relative = absolute_path.relative_to(absolute_root)
        except ValueError:
            continue
        return contained_path(absolute_root, relative.as_posix())
    msg = f"Path {path} is not under any allowed root"
    raise PathSafetyError(msg)


def allowed_extension(path: Path, extensions: set[str]) -> bool:
    """Return ``True`` if *path* has one of the allowed lowercase extensions."""
    return path.suffix.lower() in extensions


def assert_not_symlink(path: Path) -> None:
    """Raise if *path* is a symbolic link."""
    if path.is_symlink():
        msg = f"Symlinks are not allowed: {path}"
        raise PathSafetyError(msg)
