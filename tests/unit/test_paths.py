from pathlib import Path

import pytest

from ferumind.core.paths import (
    PathSafetyError,
    WorkspaceRoot,
    assert_no_symlink_escape,
    contained_path,
    is_under_root,
    resolve_repo_root,
    resolve_workspace_root,
)


def test_resolve_repo_root_finds_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    repo = resolve_repo_root(tmp_path)
    assert repo == tmp_path.resolve()
    assert isinstance(repo, Path)


def test_resolve_repo_root_walks_up(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    repo = resolve_repo_root(nested)
    assert repo == tmp_path.resolve()


def test_resolve_repo_root_raises_if_not_found(tmp_path: Path) -> None:
    with pytest.raises(PathSafetyError):
        resolve_repo_root(tmp_path)


def test_resolve_workspace_root_default() -> None:
    from ferumind.core.paths import RepoRoot

    repo = RepoRoot(Path("/repo"))
    ws = resolve_workspace_root(repo)
    assert ws == WorkspaceRoot(Path("/repo/workspace").resolve())


def test_resolve_workspace_root_custom() -> None:
    from ferumind.core.paths import RepoRoot

    repo = RepoRoot(Path("/repo"))
    ws = resolve_workspace_root(repo, "data")
    assert ws == WorkspaceRoot(Path("/repo/data").resolve())


def test_contained_path_normal(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "subdir").mkdir()

    result = contained_path(root, "subdir/file.md")
    assert result == (root / "subdir/file.md").resolve()


def test_contained_path_rejects_dotdot(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    with pytest.raises(PathSafetyError):
        contained_path(root, "../etc/passwd")


def test_contained_path_rejects_absolute(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    with pytest.raises(PathSafetyError):
        contained_path(root, "/etc/passwd")


@pytest.mark.parametrize(
    "unsafe",
    ("subdir//file.md", "subdir/./file.md", "subdir/file.md/", "subdir\\file.md", "bad\nfile.md"),
)
def test_contained_path_rejects_noncanonical_or_control_paths(tmp_path: Path, unsafe: str) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    with pytest.raises(PathSafetyError):
        contained_path(root, unsafe)


def test_contained_path_rejects_nested_dotdot(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "subdir").mkdir()

    with pytest.raises(PathSafetyError):
        contained_path(root, "subdir/../../etc/passwd")


def test_contained_path_rejects_dotdot_even_when_result_would_stay_in_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    (root / "canvases").mkdir(parents=True)

    with pytest.raises(PathSafetyError):
        contained_path(root, "canvases/../archive/secret.md")


def test_assert_no_symlink_escape_allowed(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    nested = root / "subdir"
    nested.mkdir()

    assert_no_symlink_escape(nested / "file.md", root)
    assert_no_symlink_escape(nested, root)


def test_assert_no_symlink_escape_rejects_outside(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "file.md"
    outside_file.write_text("")

    with pytest.raises(PathSafetyError):
        assert_no_symlink_escape(outside_file, root)


def test_is_under_root_accepts_normal(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    target = nested / "file.md"
    target.write_text("")

    assert is_under_root(target, root) is True


def test_is_under_root_accepts_root_itself(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    assert is_under_root(root, root) is True


def test_is_under_root_rejects_outside(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    assert is_under_root(outside, root) is False


def test_is_under_root_rejects_sibling_prefix(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    evil = tmp_path / "repo-evil"
    evil.mkdir()

    assert is_under_root(evil, root) is False


def test_is_under_root_rejects_sibling_prefix_nested(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    evil = tmp_path / "workspace_evil"
    evil.mkdir()

    assert is_under_root(evil, root) is False


def test_is_under_root_rejects_dotdot(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    candidate = root / ".." / "outside"

    assert is_under_root(candidate, root) is False


def test_is_under_root_rejects_absolute(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    assert is_under_root(Path("/etc"), root) is False


def test_resolve_workspace_root_rejects_sibling_prefix(tmp_path: Path) -> None:
    from ferumind.core.paths import RepoRoot

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "pyproject.toml").write_text("")

    evil = tmp_path / "repo-evil"
    evil.mkdir()
    evil_workspace = evil / "workspace"
    evil_workspace.mkdir(parents=True)

    repo = RepoRoot(repo_dir)
    with pytest.raises(PathSafetyError, match="escapes"):
        resolve_workspace_root(repo, "../repo-evil/workspace")


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="platform does not support symlinks")
def test_assert_no_symlink_escape_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "secret.txt"
    outside_file.write_text("secret")

    projects = root / "projects" / "demo"
    projects.mkdir(parents=True)
    escape = projects / "escape"
    escape.symlink_to(outside)

    candidate = escape / "secret.txt"
    with pytest.raises(PathSafetyError):
        assert_no_symlink_escape(candidate, root)


def test_contained_path_accepts_root_itself(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    result = contained_path(root, ".")
    assert result == root.resolve()


def test_contained_path_rejects_in_root_symlink(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    source = root / "archive"
    source.mkdir(parents=True)
    (source / "secret.md").write_text("secret", encoding="utf-8")
    alias = root / "canvases"
    alias.symlink_to(source, target_is_directory=True)

    with pytest.raises(PathSafetyError, match="Symlinks"):
        contained_path(root, "canvases/secret.md")


@pytest.mark.parametrize("bad_path", ["", "bad\x00name", "x" * 256])
def test_contained_path_rejects_unsafe_lengths_and_nul(tmp_path: Path, bad_path: str) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with pytest.raises(PathSafetyError):
        contained_path(root, bad_path)
