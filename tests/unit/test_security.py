from pathlib import Path

import pytest

from lattice.core.paths import PathSafetyError
from lattice.core.security import (
    allowed_extension,
    assert_not_symlink,
    validate_path_safety,
)


def test_validate_path_safety_in_root(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    nested = root / "subdir"
    nested.mkdir()
    target = nested / "file.md"
    target.write_text("")

    result = validate_path_safety(target, [root])
    assert result == target.resolve()


def test_validate_path_safety_rejects_outside(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "file.md"
    target.write_text("")

    with pytest.raises(PathSafetyError):
        validate_path_safety(target, [root])


def test_validate_path_safety_rejects_sibling_prefix(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    evil = tmp_path / "repo-evil"
    evil.mkdir()
    target = evil / "file.md"
    target.write_text("")

    with pytest.raises(PathSafetyError):
        validate_path_safety(target, [root])


def test_validate_path_safety_rejects_in_root_symlink(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "target.md"
    target.write_text("")
    link = root / "link.md"
    link.symlink_to(target)

    with pytest.raises(PathSafetyError):
        validate_path_safety(link, [root])


def test_allowed_extension_match() -> None:
    path = Path("doc.md")
    assert allowed_extension(path, {".md"}) is True


def test_allowed_extension_no_match() -> None:
    path = Path("doc.pdf")
    assert allowed_extension(path, {".md"}) is False


def test_allowed_extension_case_insensitive() -> None:
    path = Path("doc.MD")
    assert allowed_extension(path, {".md"}) is True


def test_assert_not_symlink_plain_file(tmp_path: Path) -> None:
    path = tmp_path / "plain.txt"
    path.write_text("hello")
    assert_not_symlink(path)


def test_assert_not_symlink_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("hello")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    with pytest.raises(PathSafetyError):
        assert_not_symlink(link)
