from pathlib import Path

import pytest
from bootstrap_workspace import bootstrap

from ferumind.core.errors import FormatUnsupportedError
from ferumind.core.paths import WorkspaceRoot

EXPECTED_DIRECTORIES = [
    "src/ferumind/core",
    "src/ferumind/mcp",
    "src/ferumind/cli",
    "src/ferumind/workers",
    "src/ferumind/db/migrations",
    "tests/unit",
    "tests/integration",
    "tests/fixtures",
    "docs",
    "workspace",
    ".githooks",
    "scripts",
    ".opencode/commands",
    ".opencode/skills/python-principal-engineer",
    ".opencode/skills/testing-and-quality",
    ".opencode/skills/mcp-hardening",
    ".opencode/skills/safe-filesystem",
]


def test_scaffold_directories_exist() -> None:
    repo_root = Path(__file__).resolve().parent.parent.parent
    missing: list[str] = []
    for rel in EXPECTED_DIRECTORIES:
        if not (repo_root / rel).is_dir():
            missing.append(rel)
    assert not missing, f"Missing directories: {missing}"


def test_bootstrap_creates_private_workspace_contract(workspace: WorkspaceRoot) -> None:
    assert workspace.stat().st_mode & 0o777 == 0o700
    assert (workspace / "system").stat().st_mode & 0o777 == 0o700
    assert (workspace / "system/meta.yml").stat().st_mode & 0o777 == 0o600


def test_force_bootstrap_preserves_registry_and_workspace_identity(
    workspace: WorkspaceRoot,
) -> None:
    registry = workspace / "system/projects.yml"
    marker = workspace / "system/meta.yml"
    pointer = workspace / "AGENTS.md"
    registry.write_text("projects:\n  durable: {}\n", encoding="utf-8")
    marker.write_text('format: 2\ncreated: "2000-01-01"\n', encoding="utf-8")
    pointer.write_text("local operator guidance\n", encoding="utf-8")

    bootstrap(workspace, force=True)

    assert registry.read_text(encoding="utf-8") == "projects:\n  durable: {}\n"
    assert marker.read_text(encoding="utf-8") == 'format: 2\ncreated: "2000-01-01"\n'
    assert pointer.read_text(encoding="utf-8") == "local operator guidance\n"


@pytest.mark.parametrize("format_version", [1, 3])
@pytest.mark.parametrize("force", [False, True])
def test_bootstrap_refuses_mismatched_workspace_without_changes(
    workspace: WorkspaceRoot,
    format_version: int,
    force: bool,
) -> None:
    marker = workspace / "system/meta.yml"
    contract = workspace / "system/rules/00-contract.md"
    marker.write_text(f"format: {format_version}\n", encoding="utf-8")
    contract.write_text("keep this exact contract\n", encoding="utf-8")

    with pytest.raises(FormatUnsupportedError):
        bootstrap(workspace, force=force)

    assert marker.read_text(encoding="utf-8") == f"format: {format_version}\n"
    assert contract.read_text(encoding="utf-8") == "keep this exact contract\n"


@pytest.mark.parametrize("force", [False, True])
def test_bootstrap_refuses_markerless_existing_workspace_without_changes(
    tmp_path: Path,
    force: bool,
) -> None:
    legacy = tmp_path / "legacy-workspace"
    legacy.mkdir()
    sentinel = legacy / "legacy-knowledge.md"
    sentinel.write_text("must survive exactly\n", encoding="utf-8")

    with pytest.raises(FormatUnsupportedError, match="ferumind migrate"):
        bootstrap(legacy, force=force)

    assert sentinel.read_text(encoding="utf-8") == "must survive exactly\n"
    assert not (legacy / "system/meta.yml").exists()
    assert not (legacy / ".ferumind").exists()
