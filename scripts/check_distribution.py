#!/usr/bin/env python3
"""Inspect built distributions for forbidden content and source drift."""

from __future__ import annotations

import stat
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from check_public_tree import forbidden_public_path_reason

REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_WHEEL_BYTES = 5 * 1024 * 1024
MAX_SDIST_BYTES = 10 * 1024 * 1024

SDIST_REQUIRED = frozenset(
    {
        ".github/dependabot.yml",
        ".github/workflows/ci.yml",
        ".github/workflows/codeql.yml",
        ".opencode/package-lock.json",
        ".opencode/package.json",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
        "SECURITY.md",
        "product/spec-mcp.md",
        "pyproject.toml",
        "uv.lock",
    }
)
WHEEL_REQUIRED = frozenset(
    {
        "ferumind/__init__.py",
        "ferumind/dashboard/static/basecoat/README.md",
        "ferumind/dashboard/static/basecoat/REVISION",
        "ferumind/dashboard/static/basecoat/base.css",
        "ferumind/dashboard/static/basecoat/components.css",
        "ferumind/dashboard/static/basecoat/tokens.css",
        "ferumind/dashboard/static/dashboard.css",
        "ferumind/dashboard/static/dashboard.js",
        "ferumind/dashboard/static/index.html",
        "ferumind/db/migrations/0001_index_observation_correlation_id.sql",
        "ferumind/db/migrations/0002_section_index.sql",
        "ferumind/db/schema.sql",
        "ferumind/py.typed",
    }
)

RUNTIME_DATA = tuple(sorted(WHEEL_REQUIRED - {"ferumind/__init__.py"}))


def _safe_archive_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and "\\" not in name


def _normalized_sdist_names(names: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    roots = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
    if len(roots) != 1:
        raise ValueError("sdist must have exactly one top-level directory")
    root = roots.pop()
    prefix = f"{root}/"
    normalized = tuple(name.removeprefix(prefix) for name in names if name != root)
    return root, normalized


def _forbidden_archive_entries(names: tuple[str, ...]) -> tuple[str, ...]:
    violations: list[str] = []
    for name in names:
        if not _safe_archive_name(name):
            violations.append(f"{name}: unsafe archive path")
            continue
        reason = forbidden_public_path_reason(name)
        if reason is not None:
            violations.append(f"{name}: {reason}")
    return tuple(violations)


def _inspect_wheel(wheel: Path) -> tuple[str, ...]:
    violations: list[str] = []
    if wheel.stat().st_size > MAX_WHEEL_BYTES:
        violations.append(f"{wheel}: exceeds {MAX_WHEEL_BYTES} bytes")
    with zipfile.ZipFile(wheel) as archive:
        names = tuple(archive.namelist())
        violations.extend(_forbidden_archive_entries(names))
        for info in archive.infolist():
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                violations.append(f"{wheel}: symlink entry is forbidden: {info.filename}")
        missing = sorted(path for path in WHEEL_REQUIRED if path not in names)
        violations.extend(f"{wheel}: missing required entry {path}" for path in missing)

        for source in sorted((REPO_ROOT / "src" / "ferumind").rglob("*.py")):
            relative = source.relative_to(REPO_ROOT / "src").as_posix()
            if relative not in names:
                violations.append(f"{wheel}: missing runtime source {relative}")
                continue
            if archive.read(relative) != source.read_bytes():
                violations.append(f"{wheel}: stale runtime source {relative}")
        for relative in RUNTIME_DATA:
            source = REPO_ROOT / "src" / relative
            if relative in names and archive.read(relative) != source.read_bytes():
                violations.append(f"{wheel}: stale runtime data {relative}")
    return tuple(violations)


def _inspect_sdist(sdist: Path) -> tuple[str, ...]:
    violations: list[str] = []
    if sdist.stat().st_size > MAX_SDIST_BYTES:
        violations.append(f"{sdist}: exceeds {MAX_SDIST_BYTES} bytes")
    with tarfile.open(sdist, "r:gz") as archive:
        members = tuple(archive.getmembers())
        raw_names = tuple(member.name for member in members)
        for name in raw_names:
            if not _safe_archive_name(name):
                violations.append(f"{name}: unsafe archive path")
        for member in members:
            if not member.isfile() and not member.isdir():
                violations.append(f"{sdist}: link or special entry is forbidden: {member.name}")
        try:
            root, names = _normalized_sdist_names(raw_names)
        except ValueError as exc:
            return (*violations, f"{sdist}: {exc}")
        violations.extend(_forbidden_archive_entries(names))
        missing = sorted(path for path in SDIST_REQUIRED if path not in names)
        violations.extend(f"{sdist}: missing required entry {path}" for path in missing)

        for relative in sorted(names):
            source = REPO_ROOT / relative
            if not source.is_file():
                continue
            member = archive.extractfile(f"{root}/{relative}")
            if member is None:
                violations.append(f"{sdist}: source entry is not a file: {relative}")
                continue
            if member.read() != source.read_bytes():
                violations.append(f"{sdist}: stale source entry {relative}")
    return tuple(violations)


def inspect_distributions(dist_dir: Path) -> tuple[str, ...]:
    """Return violations for exactly one wheel and one source distribution."""
    wheels = tuple(sorted(dist_dir.glob("*.whl")))
    sdists = tuple(sorted(dist_dir.glob("*.tar.gz")))
    violations: list[str] = []
    if len(wheels) != 1:
        violations.append(f"{dist_dir}: expected exactly one wheel, found {len(wheels)}")
    if len(sdists) != 1:
        violations.append(f"{dist_dir}: expected exactly one sdist, found {len(sdists)}")
    if len(wheels) == 1:
        violations.extend(_inspect_wheel(wheels[0]))
    if len(sdists) == 1:
        violations.extend(_inspect_sdist(sdists[0]))
    return tuple(violations)


def main() -> int:
    dist_dir = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else REPO_ROOT / "dist"
    violations = inspect_distributions(dist_dir)
    if violations:
        print("Distribution inspection failed:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1
    print("Distribution contents and source freshness checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
