"""The CI matrix resolver: selection logic, failure modes, and the index seam.

Nothing here touches the network. The one network call in
``scripts/mcp_matrix_versions.py`` lives behind :func:`fetch_published_versions`
and is exercised through ``httpx.MockTransport``; every other function takes its
release data as an argument.

The behaviour these tests pin down is why the matrix is one row today and will
be more rows tomorrow without anyone editing it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from mcp_matrix_versions import (
    MatrixResolution,
    MatrixResolutionError,
    allowed_releases,
    declared_requirement,
    fetch_published_versions,
    github_output_lines,
    locked_version,
    main,
    resolve,
    resolve_from_repo,
    select_matrix,
)
from packaging.specifiers import SpecifierSet
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

PYPROJECT = """
[project]
name = "ferumind"
dependencies = [
  "pydantic>=2.0,<3",
  "mcp>=2.0.0,<3",
  "pyyaml>=6.0,<7",
]
"""

UV_LOCK = """
[[package]]
name = "pydantic"
version = "2.11.0"

[[package]]
name = "mcp"
version = "2.0.0"
"""


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _json_index(versions: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/simple/mcp/"
        assert request.headers["Accept"] == "application/vnd.pypi.simple.v1+json"
        return httpx.Response(200, json={"name": "mcp", "versions": versions})

    return handler


# ── Reading the declared range ──────────────────────────────────────────────


def test_declared_requirement_reads_the_specifier_from_pyproject() -> None:
    requirement = declared_requirement(PYPROJECT)
    assert requirement.name == "mcp"
    assert Version("2.0.0") in requirement.specifier
    assert Version("3.0.0") not in requirement.specifier


def test_the_real_repository_declares_a_bounded_mcp_range() -> None:
    """The matrix is only meaningful if the shipped range is the one it reads."""
    requirement = declared_requirement((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert requirement.name == "mcp"
    assert Version("2.0.0") in requirement.specifier
    assert Version("3.0.0") not in requirement.specifier


def test_declared_requirement_matches_an_extra_bearing_declaration() -> None:
    """``mcp[cli]`` is a different string but the same project."""
    requirement = declared_requirement('[project]\ndependencies = ["mcp[cli]>=2.0.0,<3"]\n')
    assert requirement.name == "mcp"


def test_declared_requirement_rejects_a_missing_dependency() -> None:
    with pytest.raises(MatrixResolutionError, match="declares no 'mcp' dependency"):
        declared_requirement('[project]\ndependencies = ["pydantic>=2.0,<3"]\n')


def test_declared_requirement_rejects_a_duplicate_dependency() -> None:
    with pytest.raises(MatrixResolutionError, match="more than once"):
        declared_requirement('[project]\ndependencies = ["mcp>=2.0.0,<3", "mcp<2.5"]\n')


def test_declared_requirement_rejects_an_unbounded_dependency() -> None:
    """An unpinned dependency would ask the matrix to test every release ever."""
    with pytest.raises(MatrixResolutionError, match="no version specifier"):
        declared_requirement('[project]\ndependencies = ["mcp"]\n')


def test_declared_requirement_rejects_a_missing_project_table() -> None:
    with pytest.raises(MatrixResolutionError, match=r"no \[project\] table"):
        declared_requirement("[tool.ruff]\nline-length = 100\n")


def test_declared_requirement_rejects_unparsable_toml() -> None:
    with pytest.raises(MatrixResolutionError, match="not valid TOML"):
        declared_requirement("[project\n")


# ── Reading the lock ────────────────────────────────────────────────────────


def test_locked_version_reads_the_pinned_release() -> None:
    assert locked_version(UV_LOCK) == Version("2.0.0")


def test_the_real_lock_pins_a_version_the_real_range_allows() -> None:
    requirement = declared_requirement((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    locked = locked_version((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert locked in requirement.specifier


def test_locked_version_rejects_a_missing_package() -> None:
    with pytest.raises(MatrixResolutionError, match="pins no 'mcp' package"):
        locked_version('[[package]]\nname = "pydantic"\nversion = "2.11.0"\n')


def test_locked_version_rejects_conflicting_pins() -> None:
    lock = (
        '[[package]]\nname = "mcp"\nversion = "2.0.0"\n'
        '[[package]]\nname = "mcp"\nversion = "2.1.0"\n'
    )
    with pytest.raises(MatrixResolutionError, match="several versions"):
        locked_version(lock)


def test_locked_version_rejects_an_unparsable_version() -> None:
    with pytest.raises(MatrixResolutionError, match="pins 'mcp' at"):
        locked_version('[[package]]\nname = "mcp"\nversion = "not-a-version"\n')


def test_locked_version_rejects_a_lock_without_packages() -> None:
    with pytest.raises(MatrixResolutionError, match=r"no \[\[package\]\] entries"):
        locked_version("version = 1\n")


# ── Candidate selection ─────────────────────────────────────────────────────


def test_pre_releases_are_never_matrix_candidates() -> None:
    """Ferumind does not claim support on an alpha, beta, rc, or dev build.

    This is the whole reason today's matrix is one row: every 2.x artifact
    other than 2.0.0 on PyPI is a pre-release.
    """
    published = ["2.0.0a1", "2.0.0a2", "2.0.0a3", "2.0.0b1", "2.0.0b2", "2.0.0rc1", "2.0.0"]
    assert allowed_releases(published, SpecifierSet(">=2.0.0,<3")) == (Version("2.0.0"),)


def test_dev_releases_are_never_matrix_candidates() -> None:
    assert allowed_releases(["2.1.0.dev1", "2.0.0"], SpecifierSet(">=2.0.0,<3")) == (
        Version("2.0.0"),
    )


def test_post_releases_are_ordinary_candidates() -> None:
    """A post-release is a stable artifact, unlike a pre-release."""
    assert allowed_releases(["2.0.0", "2.0.0.post1"], SpecifierSet(">=2.0.0,<3")) == (
        Version("2.0.0"),
        Version("2.0.0.post1"),
    )


def test_out_of_range_and_unparsable_releases_are_dropped() -> None:
    published = ["1.29.0", "2.0.0", "3.0.0", "not-a-version", ""]
    assert allowed_releases(published, SpecifierSet(">=2.0.0,<3")) == (Version("2.0.0"),)


def test_candidates_order_by_pep_440_not_lexically() -> None:
    """``2.10.0`` is newer than ``2.9.0``; string sorting says otherwise.

    Hand-rolled comparison is exactly how a matrix picks the wrong "highest
    allowed" row and proves the wrong thing.
    """
    allowed = allowed_releases(["2.9.0", "2.10.0", "2.2.0"], SpecifierSet(">=2.0.0,<3"))
    assert [str(version) for version in allowed] == ["2.2.0", "2.9.0", "2.10.0"]


def test_one_allowed_release_collapses_to_a_single_row() -> None:
    """Lowest, locked, and highest are the same version. One row is the truth.

    A hardcoded three-row matrix here would install 2.0.0 three times and
    report three green rows for one version's worth of evidence.
    """
    assert select_matrix([Version("2.0.0")], Version("2.0.0")) == ("2.0.0",)


def test_a_wider_range_produces_lowest_locked_and_highest() -> None:
    """The same code returns three rows the day the range admits three."""
    allowed = [Version("2.0.0"), Version("2.1.0"), Version("2.2.0"), Version("2.3.0")]
    assert select_matrix(allowed, Version("2.1.0")) == ("2.0.0", "2.1.0", "2.3.0")


def test_a_locked_floor_produces_two_rows() -> None:
    allowed = [Version("2.0.0"), Version("2.1.0"), Version("2.2.0")]
    assert select_matrix(allowed, Version("2.0.0")) == ("2.0.0", "2.2.0")


def test_an_empty_candidate_set_is_an_error_not_an_empty_matrix() -> None:
    """Zero rows would report green having proved nothing at all."""
    with pytest.raises(MatrixResolutionError, match="zero rows"):
        select_matrix([], Version("2.0.0"))


# ── End-to-end resolution ───────────────────────────────────────────────────


def test_resolve_returns_one_row_for_todays_published_releases() -> None:
    published = ["1.29.0", "2.0.0a3", "2.0.0rc1", "2.0.0"]
    resolution = resolve(PYPROJECT, UV_LOCK, published)
    assert resolution.versions == ("2.0.0",)
    assert resolution.locked == "2.0.0"
    assert Version("2.0.0") in SpecifierSet(resolution.specifier)


def test_resolve_widens_by_itself_when_the_index_gains_releases() -> None:
    """No code change turns this matrix from one row into three."""
    published = ["2.0.0", "2.1.0", "2.4.0", "2.5.0b1", "3.0.0"]
    resolution = resolve(PYPROJECT, UV_LOCK, published)
    assert resolution.versions == ("2.0.0", "2.4.0")


def test_resolve_rejects_a_lock_the_declared_range_forbids() -> None:
    """A contradiction between the cap and the lock must stop the matrix."""
    lock = '[[package]]\nname = "mcp"\nversion = "1.29.0"\n'
    with pytest.raises(MatrixResolutionError, match="does not allow"):
        resolve(PYPROJECT, lock, ["1.29.0", "2.0.0"])


def test_every_resolved_row_satisfies_the_declared_specifier() -> None:
    published = ["2.0.0", "2.1.0", "2.4.0"]
    resolution = resolve(PYPROJECT, UV_LOCK, published)
    specifier = SpecifierSet(resolution.specifier)
    assert all(Version(version) in specifier for version in resolution.versions)


# ── The index seam ──────────────────────────────────────────────────────────


def test_fetch_published_versions_reads_the_pep_700_versions_key() -> None:
    with _client(_json_index(["1.29.0", "2.0.0"])) as client:
        versions = fetch_published_versions(index_url="https://pypi.org/simple", client=client)
    assert versions == ("1.29.0", "2.0.0")


def test_fetch_published_versions_tolerates_a_trailing_slash_on_the_index() -> None:
    with _client(_json_index(["2.0.0"])) as client:
        assert fetch_published_versions(index_url="https://pypi.org/simple/", client=client) == (
            "2.0.0",
        )


def test_fetch_published_versions_fails_when_the_index_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed", request=request)

    with _client(handler) as client, pytest.raises(MatrixResolutionError, match="could not read"):
        fetch_published_versions(client=client)


def test_fetch_published_versions_fails_on_an_index_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    with _client(handler) as client, pytest.raises(MatrixResolutionError, match="could not read"):
        fetch_published_versions(client=client)


def test_fetch_published_versions_fails_on_a_non_json_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>simple index</html>")

    with _client(handler) as client, pytest.raises(MatrixResolutionError, match="did not return"):
        fetch_published_versions(client=client)


def test_fetch_published_versions_rejects_an_index_without_a_versions_key() -> None:
    """A PEP 691 index predating PEP 700 must be refused, not guessed at."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"name": "mcp", "files": []})

    with _client(handler) as client, pytest.raises(MatrixResolutionError, match="PEP 700"):
        fetch_published_versions(client=client)


def test_resolve_from_repo_reads_the_real_checkout_without_network() -> None:
    with _client(_json_index(["1.29.0", "2.0.0rc1", "2.0.0"])) as client:
        resolution = resolve_from_repo(REPO_ROOT, client=client)
    assert resolution.versions == ("2.0.0",)


def test_resolve_from_repo_reports_a_missing_checkout(tmp_path: Path) -> None:
    with pytest.raises(MatrixResolutionError, match=r"pyproject\.toml is missing"):
        resolve_from_repo(tmp_path)


# ── Command-line surface ────────────────────────────────────────────────────


def test_github_output_lines_are_single_line_and_fromjson_ready() -> None:
    resolution = MatrixResolution(specifier=">=2.0.0,<3", locked="2.0.0", versions=("2.0.0",))
    lines = github_output_lines(resolution)
    assert lines[0] == 'versions=["2.0.0"]'
    assert json.loads(lines[0].removeprefix("versions=")) == ["2.0.0"]
    assert all("\n" not in line for line in lines)


def test_main_fails_loudly_instead_of_emitting_an_empty_matrix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A resolver that cannot read the range must exit non-zero and say why."""
    exit_code = main(["--repo-root", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "matrix resolution failed" in captured.err
