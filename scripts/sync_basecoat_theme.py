#!/usr/bin/env python3
"""Refresh Ferumind's vendored Basecoat CSS from a clean local checkout.

This command deliberately has no network behavior. It snapshots the three canonical
theme blobs at the checkout's current ``HEAD`` and writes ``REVISION`` last so that
the revision never advertises CSS that was not copied successfully.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDORED_THEME_ROOT = REPO_ROOT / "src" / "ferumind" / "dashboard" / "static" / "basecoat"

THEME_SOURCE_PATHS = (
    "packages/theme/src/tokens.css",
    "packages/theme/src/base.css",
    "packages/theme/src/components.css",
)
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
GIT_PORCELAIN_FORMAT = "v" + "1"


class BasecoatSyncError(RuntimeError):
    """The requested checkout cannot produce a trustworthy theme snapshot."""


@dataclass(frozen=True)
class SyncResult:
    """The source revision and destination files changed by one synchronization."""

    revision: str
    changed_files: tuple[str, ...]


def _git_output(source_root: Path, *arguments: str) -> bytes:
    """Run one fixed, read-only Git query and return its stdout."""
    git = shutil.which("git")
    if git is None:
        raise BasecoatSyncError("git is required to synchronize the Basecoat theme")
    # Git exports repository-local variables while running hooks. Without an
    # isolated environment, ``git -C source_root`` can still address the
    # caller's repository/index instead of the Basecoat checkout.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(  # noqa: S603 - fixed read-only Git queries, never a shell
        [git, "-C", str(source_root), *arguments],
        check=False,
        capture_output=True,
        env=environment,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise BasecoatSyncError(f"Basecoat checkout validation failed{suffix}")
    return result.stdout


def _resolved_checkout(source_root: Path) -> Path:
    """Return a source path proven to be the root of a Git worktree."""
    try:
        resolved = source_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise BasecoatSyncError(f"Basecoat source does not exist: {source_root}") from exc
    if not resolved.is_dir():
        raise BasecoatSyncError(f"Basecoat source is not a directory: {source_root}")

    top_level_raw = _git_output(resolved, "rev-parse", "--show-toplevel")
    try:
        top_level = Path(top_level_raw.decode("utf-8").strip()).resolve(strict=True)
    except (OSError, UnicodeDecodeError) as exc:
        raise BasecoatSyncError("Git returned an invalid Basecoat worktree root") from exc
    if top_level != resolved:
        raise BasecoatSyncError(
            f"Basecoat source must be the checkout root ({top_level}), not {resolved}"
        )
    return resolved


def _source_revision(source_root: Path) -> str:
    """Return the checkout's full current commit SHA."""
    raw_revision = _git_output(source_root, "rev-parse", "--verify", "HEAD^{commit}")
    try:
        revision = raw_revision.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise BasecoatSyncError("Git returned a non-ASCII Basecoat revision") from exc
    if COMMIT_SHA.fullmatch(revision) is None:
        raise BasecoatSyncError(f"Git returned an invalid Basecoat revision: {revision!r}")
    return revision


def _require_clean_theme_sources(source_root: Path) -> None:
    """Refuse changes to theme inputs while allowing unrelated checkout changes."""
    status = _git_output(
        source_root,
        "status",
        f"--porcelain={GIT_PORCELAIN_FORMAT}",
        "-z",
        "--untracked-files=all",
        "--",
        *THEME_SOURCE_PATHS,
    )
    if status:
        raise BasecoatSyncError(
            "Basecoat theme sources have staged, unstaged, or untracked changes; "
            "commit or restore the three canonical CSS files before synchronizing"
        )


def _committed_theme(source_root: Path) -> dict[str, bytes]:
    """Read the canonical theme directly from ``HEAD`` as committed blobs."""
    contents: dict[str, bytes] = {}
    for relative_path in THEME_SOURCE_PATHS:
        object_type = _git_output(source_root, "cat-file", "-t", f"HEAD:{relative_path}")
        if object_type.strip() != b"blob":
            raise BasecoatSyncError(
                f"Basecoat theme source is not a committed regular file: {relative_path}"
            )
        contents[relative_path] = _git_output(source_root, "show", f"HEAD:{relative_path}")
    return contents


def _atomic_write(destination: Path, content: bytes) -> bool:
    """Replace *destination* atomically when its bytes differ."""
    try:
        if destination.read_bytes() == content:
            return False
    except FileNotFoundError:
        pass

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def sync_basecoat_theme(
    source_root: Path, destination_root: Path = VENDORED_THEME_ROOT
) -> SyncResult:
    """Synchronize canonical Basecoat theme blobs into Ferumind.

    Only the three relevant source paths must be clean; unrelated work in the Basecoat
    checkout does not affect the committed theme snapshot. ``REVISION`` is replaced
    atomically after every required CSS write succeeds.
    """
    checkout = _resolved_checkout(source_root)
    revision = _source_revision(checkout)
    _require_clean_theme_sources(checkout)
    theme = _committed_theme(checkout)

    changed: list[str] = []
    for source_path in THEME_SOURCE_PATHS:
        destination_name = Path(source_path).name
        if _atomic_write(destination_root / destination_name, theme[source_path]):
            changed.append(destination_name)
    if _atomic_write(destination_root / "REVISION", f"{revision}\n".encode("ascii")):
        changed.append("REVISION")
    return SyncResult(revision=revision, changed_files=tuple(changed))


def main() -> int:
    """Parse the local source path, synchronize it, and report changed files."""
    parser = argparse.ArgumentParser(
        description="Vendor canonical Basecoat CSS from a clean local checkout (no network)"
    )
    parser.add_argument("--source", required=True, type=Path, help="path to a Basecoat checkout")
    arguments = parser.parse_args()

    try:
        result = sync_basecoat_theme(arguments.source)
    except BasecoatSyncError as exc:
        print(f"Basecoat theme sync refused: {exc}", file=sys.stderr)
        return 2

    if result.changed_files:
        print(f"Synced Basecoat {result.revision}:")
        for changed_file in result.changed_files:
            print(f"  updated {changed_file}")
    else:
        print(f"Basecoat theme is already current at {result.revision}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
