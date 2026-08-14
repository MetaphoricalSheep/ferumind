#!/usr/bin/env python3
"""Resolve the MCP SDK versions the CI compatibility matrix must run.

The matrix proves that every version of the SDK ``pyproject.toml`` allows
actually works against Ferumind's two private attachment points
(``_lowlevel_server`` and ``_tool_manager``, see
:mod:`ferumind.mcp.sdk_internals`). Three rows are interesting: the lowest
version the declared specifier admits, the version ``uv.lock`` pins — the only
one a source checkout installs — and the highest version the specifier admits.

**Why this resolves at run time instead of listing versions.** Today
``mcp>=2.0.0,<3`` admits exactly one stable release, ``2.0.0``: every other 2.x
artifact on PyPI is an alpha, beta, or release candidate, and Ferumind does not
support running on a pre-release SDK. Lowest, locked, and highest are therefore
the same version, and this script emits **one row**.

That is deliberate and it is the honest answer. A hardcoded three-row matrix
would install ``2.0.0`` three times and report three green rows for one
version's worth of evidence — exactly the failure REL-021 hit when a floor test
passed while running against a version it had never installed. Do not "fix" the
single row by hardcoding versions. The day ``mcp`` 2.1.0 ships, this script
returns two rows, and a further release returns three, with no code change.

Widening the cap in ``pyproject.toml`` therefore widens the matrix by itself.
See ``docs/mcp-sdk-support.md`` for the review that widening requires.

Fails loudly rather than emitting an empty or partial matrix: a matrix that
silently runs zero rows is worse than no matrix, because it reports green.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parent.parent
PROJECT = "mcp"
DEFAULT_INDEX_URL = "https://pypi.org/simple"
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0

#: PEP 691 JSON simple API. Its ``versions`` key arrived with PEP 700, so an
#: index that only speaks the HTML API is rejected rather than guessed at.
SIMPLE_JSON_ACCEPT = "application/vnd.pypi.simple.v1+json"


class MatrixResolutionError(RuntimeError):
    """The version set could not be resolved, so no matrix should run."""


@dataclass(frozen=True)
class MatrixResolution:
    """The resolved matrix: what was declared, what is locked, what runs."""

    specifier: str
    locked: str
    versions: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-serializable form written to stdout."""
        return {
            "specifier": self.specifier,
            "locked": self.locked,
            "versions": list(self.versions),
        }


def _project_dependencies(pyproject_text: str) -> tuple[str, ...]:
    """Return the ``[project].dependencies`` strings."""
    try:
        document = cast("dict[str, object]", tomllib.loads(pyproject_text))
    except tomllib.TOMLDecodeError as exc:
        raise MatrixResolutionError(f"pyproject.toml is not valid TOML: {exc}") from exc
    project = document.get("project")
    if not isinstance(project, dict):
        raise MatrixResolutionError("pyproject.toml has no [project] table")
    dependencies = cast("dict[str, object]", project).get("dependencies")
    if not isinstance(dependencies, list):
        raise MatrixResolutionError("pyproject.toml declares no [project] dependencies list")
    return tuple(item for item in cast("list[object]", dependencies) if isinstance(item, str))


def declared_requirement(pyproject_text: str, project: str = PROJECT) -> Requirement:
    """Return the declared requirement for *project*, specifier included.

    The specifier is never hardcoded here: widening or narrowing the cap in
    ``pyproject.toml`` is the only way to change which versions run.
    """
    wanted = canonicalize_name(project)
    matches: list[Requirement] = []
    for raw in _project_dependencies(pyproject_text):
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            continue
        if canonicalize_name(requirement.name) == wanted:
            matches.append(requirement)
    if not matches:
        raise MatrixResolutionError(
            f"pyproject.toml declares no {project!r} dependency, so there is no "
            "range to build a compatibility matrix from"
        )
    if len(matches) > 1:
        found = ", ".join(str(match) for match in matches)
        raise MatrixResolutionError(
            f"pyproject.toml declares {project!r} more than once ({found}); "
            "which range the matrix should prove is ambiguous"
        )
    requirement = matches[0]
    if not str(requirement.specifier):
        raise MatrixResolutionError(
            f"pyproject.toml declares {project!r} with no version specifier, so "
            "the matrix would have to test every release ever published"
        )
    return requirement


def locked_version(uv_lock_text: str, project: str = PROJECT) -> Version:
    """Return the version ``uv.lock`` pins for *project*.

    A source checkout — the only supported install path — installs exactly
    this version, so it is always a matrix row.
    """
    try:
        document = cast("dict[str, object]", tomllib.loads(uv_lock_text))
    except tomllib.TOMLDecodeError as exc:
        raise MatrixResolutionError(f"uv.lock is not valid TOML: {exc}") from exc
    packages = document.get("package")
    if not isinstance(packages, list):
        raise MatrixResolutionError("uv.lock has no [[package]] entries")

    wanted = canonicalize_name(project)
    found: list[str] = []
    for entry in cast("list[object]", packages):
        if not isinstance(entry, dict):
            continue
        table = cast("dict[str, object]", entry)
        name = table.get("name")
        version = table.get("version")
        if isinstance(name, str) and canonicalize_name(name) == wanted and isinstance(version, str):
            found.append(version)
    if not found:
        raise MatrixResolutionError(f"uv.lock pins no {project!r} package")
    if len(set(found)) > 1:
        raise MatrixResolutionError(
            f"uv.lock pins {project!r} at several versions ({', '.join(sorted(set(found)))}); "
            "the matrix cannot tell which one a checkout installs"
        )
    try:
        return Version(found[0])
    except InvalidVersion as exc:
        raise MatrixResolutionError(f"uv.lock pins {project!r} at {found[0]!r}: {exc}") from exc


def allowed_releases(published: Iterable[str], specifier: SpecifierSet) -> tuple[Version, ...]:
    """Return the ascending stable releases in *published* that *specifier* admits.

    Pre-releases are excluded on purpose. Ferumind makes no claim about running
    on an alpha, beta, or release candidate, so testing one would prove a
    support level the project does not offer. Post-releases are kept: they are
    ordinary stable artifacts.
    """
    allowed: set[Version] = set()
    for raw in published:
        try:
            version = Version(raw)
        except InvalidVersion:
            continue
        if version.is_prerelease or version.is_devrelease:
            continue
        if version in specifier:
            allowed.add(version)
    return tuple(sorted(allowed))


def select_matrix(allowed: Sequence[Version], locked: Version) -> tuple[str, ...]:
    """Return the deduplicated lowest / locked / highest rows, ascending.

    Deduplication is what makes a one-row matrix honest. When the declared
    range admits a single stable release, all three interesting versions are
    that release, and running it once is the whole of the available evidence.

    The locked version is always a row, even if the index no longer lists it —
    a yanked release that ``uv.lock`` still pins is exactly the case worth
    surfacing, and the row fails at install time where it is visible.
    """
    if not allowed:
        raise MatrixResolutionError(
            "no published stable release satisfies the declared specifier, so "
            "the matrix would run zero rows and report success for nothing"
        )
    rows = {allowed[0], locked, allowed[-1]}
    return tuple(str(version) for version in sorted(rows))


def resolve(pyproject_text: str, uv_lock_text: str, published: Iterable[str]) -> MatrixResolution:
    """Resolve the matrix from the declared range, the lock, and the index."""
    requirement = declared_requirement(pyproject_text)
    locked = locked_version(uv_lock_text)
    if locked not in requirement.specifier:
        raise MatrixResolutionError(
            f"uv.lock pins {PROJECT} {locked}, which the declared specifier "
            f"'{requirement.specifier}' does not allow. Relock, or fix the "
            "declared range — a matrix built from a contradiction proves nothing."
        )
    allowed = allowed_releases(published, requirement.specifier)
    return MatrixResolution(
        specifier=str(requirement.specifier),
        locked=str(locked),
        versions=select_matrix(allowed, locked),
    )


def fetch_published_versions(
    project: str = PROJECT,
    *,
    index_url: str = DEFAULT_INDEX_URL,
    client: httpx.Client | None = None,
) -> tuple[str, ...]:
    """Return every version *index_url* publishes for *project*.

    The only network call in this module, kept behind its own function so the
    selection logic above is tested with injected release data.
    """
    url = f"{index_url.rstrip('/')}/{canonicalize_name(project)}/"
    http = client or httpx.Client(
        timeout=httpx.Timeout(
            connect=CONNECT_TIMEOUT,
            read=READ_TIMEOUT,
            write=READ_TIMEOUT,
            pool=CONNECT_TIMEOUT,
        ),
        follow_redirects=True,
    )
    try:
        response = http.get(url, headers={"Accept": SIMPLE_JSON_ACCEPT})
        response.raise_for_status()
        payload = cast("dict[str, object]", response.json())
    except httpx.HTTPError as exc:
        raise MatrixResolutionError(f"could not read {url}: {exc}") from exc
    except ValueError as exc:
        raise MatrixResolutionError(f"{url} did not return JSON: {exc}") from exc
    finally:
        if client is None:
            http.close()

    versions = payload.get("versions")
    if not isinstance(versions, list):
        raise MatrixResolutionError(
            f"{url} returned no 'versions' key. The matrix needs a PEP 700 JSON "
            "simple index; an HTML-only index cannot be used."
        )
    return tuple(item for item in cast("list[object]", versions) if isinstance(item, str))


def github_output_lines(resolution: MatrixResolution) -> tuple[str, ...]:
    """Return ``key=value`` lines for ``$GITHUB_OUTPUT``.

    ``versions`` is a compact JSON array so the workflow can hand it straight
    to ``fromJSON``. Every value is single-line, which the file format requires.
    """
    return (
        f"versions={json.dumps(list(resolution.versions), separators=(',', ':'))}",
        f"locked={resolution.locked}",
        f"specifier={resolution.specifier}",
    )


def resolve_from_repo(
    repo_root: Path,
    *,
    index_url: str = DEFAULT_INDEX_URL,
    client: httpx.Client | None = None,
) -> MatrixResolution:
    """Read the declared range and lock from *repo_root*, then query the index."""
    pyproject = repo_root / "pyproject.toml"
    uv_lock = repo_root / "uv.lock"
    for path in (pyproject, uv_lock):
        if not path.is_file():
            raise MatrixResolutionError(f"{path} is missing")
    published = fetch_published_versions(index_url=index_url, client=client)
    return resolve(
        pyproject.read_text(encoding="utf-8"),
        uv_lock.read_text(encoding="utf-8"),
        published,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository holding pyproject.toml and uv.lock (default: this checkout)",
    )
    parser.add_argument(
        "--index-url",
        default=DEFAULT_INDEX_URL,
        help=f"PEP 691 JSON simple index to query (default: {DEFAULT_INDEX_URL})",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        default=None,
        help="Append versions/locked/specifier to this $GITHUB_OUTPUT file",
    )
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root
    index_url: str = args.index_url
    github_output: Path | None = args.github_output

    try:
        resolution = resolve_from_repo(repo_root.resolve(), index_url=index_url)
    except MatrixResolutionError as exc:
        print(f"MCP SDK matrix resolution failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(resolution.as_dict()))
    if github_output is not None:
        with github_output.open("a", encoding="utf-8") as handle:
            for line in github_output_lines(resolution):
                handle.write(f"{line}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
