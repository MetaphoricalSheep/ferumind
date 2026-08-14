"""Vendored Basecoat provenance and the offline synchronization boundary."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest
from sync_basecoat_theme import (
    THEME_SOURCE_PATHS,
    BasecoatSyncError,
    sync_basecoat_theme,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VENDORED_ROOT = REPO_ROOT / "src" / "ferumind" / "dashboard" / "static" / "basecoat"
BASECOAT_REVISION = "70369cc32afc4f5c517e09743172ba9dc36e34f8"
EXPECTED_SHA256 = {
    "tokens.css": "9b8c5c1f095186bac5aa58d3557b7a853bd32f2cf0f2b39104878e9eae7a68f2",
    "base.css": "d554b545b20e690f071d4f6bdeed0673bb2a46455eb0def312593501dfe25151",
    "components.css": "dab787b6d828cc481a1bb641c9e15b59e0d85ac8307bc1c7816284eb5f0a4364",
}


def _git(repository: Path, *arguments: str) -> str:
    git = shutil.which("git")
    if git is None:
        pytest.fail("git is required by the Basecoat synchronization tests")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(
        [git, *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


def _basecoat_checkout(root: Path) -> tuple[Path, dict[str, bytes], str]:
    root.mkdir()
    _git(root, "init", "--quiet")
    _git(root, "config", "core.autocrlf", "false")

    contents: dict[str, bytes] = {}
    for number, relative_path in enumerate(THEME_SOURCE_PATHS, start=1):
        content = f"/* canonical theme {number} */\r\n".encode()
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        contents[relative_path] = content
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Ferumind Test",
        "-c",
        "user.email=ferumind@example.invalid",
        "commit",
        "--quiet",
        "--message=theme fixture",
    )
    return root, contents, _git(root, "rev-parse", "HEAD")


def test_vendored_theme_matches_the_pinned_basecoat_revision() -> None:
    assert (VENDORED_ROOT / "REVISION").read_bytes() == f"{BASECOAT_REVISION}\n".encode()
    actual = {
        name: hashlib.sha256((VENDORED_ROOT / name).read_bytes()).hexdigest()
        for name in EXPECTED_SHA256
    }
    assert actual == EXPECTED_SHA256


def test_dashboard_package_resources_include_basecoat_provenance() -> None:
    theme = resources.files("ferumind.dashboard").joinpath("static").joinpath("basecoat")
    for name in (*EXPECTED_SHA256, "REVISION", "README.md"):
        asset = theme.joinpath(name)
        assert asset.is_file()
        assert asset.read_bytes()
    notice = theme.joinpath("README.md").read_text(encoding="utf-8")
    assert BASECOAT_REVISION in notice
    assert "do not infer that Ferumind's surrounding MIT license" in notice


def test_sync_copies_exact_committed_bytes_and_is_idempotent(tmp_path: Path) -> None:
    source, expected, revision = _basecoat_checkout(tmp_path / "basecoat")
    destination = tmp_path / "vendored"

    first = sync_basecoat_theme(source, destination)

    assert first.revision == revision
    assert first.changed_files == ("tokens.css", "base.css", "components.css", "REVISION")
    for source_path, content in expected.items():
        assert (destination / Path(source_path).name).read_bytes() == content
    assert (destination / "REVISION").read_bytes() == f"{revision}\n".encode()

    second = sync_basecoat_theme(source, destination)
    assert second.revision == revision
    assert second.changed_files == ()


def test_sync_allows_unrelated_dirty_checkout_files(tmp_path: Path) -> None:
    source, _, revision = _basecoat_checkout(tmp_path / "basecoat")
    (source / "unrelated-notes.txt").write_text("work in progress\n", encoding="utf-8")

    result = sync_basecoat_theme(source, tmp_path / "vendored")

    assert result.revision == revision


@pytest.mark.parametrize("staged", [False, True], ids=["unstaged", "staged"])
def test_sync_refuses_dirty_relevant_theme_sources(tmp_path: Path, staged: bool) -> None:
    source, _, _ = _basecoat_checkout(tmp_path / "basecoat")
    changed_path = source / THEME_SOURCE_PATHS[0]
    changed_path.write_text("/* changed locally */\n", encoding="utf-8")
    if staged:
        _git(source, "add", THEME_SOURCE_PATHS[0])
    destination = tmp_path / "vendored"

    with pytest.raises(BasecoatSyncError, match="theme sources have staged, unstaged"):
        sync_basecoat_theme(source, destination)

    assert not destination.exists()


def test_sync_requires_the_checkout_root(tmp_path: Path) -> None:
    source, _, _ = _basecoat_checkout(tmp_path / "basecoat")

    with pytest.raises(BasecoatSyncError, match="must be the checkout root"):
        sync_basecoat_theme(source / "packages", tmp_path / "vendored")
