from pathlib import Path

import pytest
from bootstrap_workspace import bootstrap

from ferumind.core.contract_install import CONTRACT_INSTALLS
from ferumind.core.errors import FormatUnsupportedError
from ferumind.core.format import SUPPORTED_FORMAT
from ferumind.core.frontmatter import parse_frontmatter, validate_description
from ferumind.core.paths import WorkspaceRoot

EXPECTED_DIRECTORIES = [
    "src/ferumind/core",
    "src/ferumind/mcp",
    "src/ferumind/cli",
    "src/ferumind/dashboard",
    "src/ferumind/db/migrations",
    "tests/unit",
    "tests/integration",
    "tests/fixtures",
    "docs",
    # No "workspace": nothing under it is tracked, so a fresh clone does not
    # have one until bootstrap creates it. Shipping a placeholder to keep the
    # directory in the tree is what made bootstrap refuse to initialize a
    # first-run checkout.
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


def test_project_templates_define_valid_descriptions() -> None:
    repo_root = Path(__file__).resolve().parent.parent.parent
    for relative in ("templates/spine.md", "templates/project-rules.md"):
        template = repo_root / "product/contract" / relative
        description = parse_frontmatter(template.read_text(encoding="utf-8")).get("description")
        assert validate_description(description)


def test_force_bootstrap_preserves_registry_and_workspace_identity(
    workspace: WorkspaceRoot,
) -> None:
    registry = workspace / "system/projects.yml"
    marker = workspace / "system/meta.yml"
    pointer = workspace / "AGENTS.md"
    registry.write_text("projects:\n  durable: {}\n", encoding="utf-8")
    marker.write_text(f'format: {SUPPORTED_FORMAT}\ncreated: "2000-01-01"\n', encoding="utf-8")
    pointer.write_text("local operator guidance\n", encoding="utf-8")

    bootstrap(workspace, force=True)

    assert registry.read_text(encoding="utf-8") == "projects:\n  durable: {}\n"
    assert marker.read_text(encoding="utf-8") == (
        f'format: {SUPPORTED_FORMAT}\ncreated: "2000-01-01"\n'
    )
    assert pointer.read_text(encoding="utf-8") == "local operator guidance\n"


def test_bootstrap_installs_the_episode_storage_convention(
    workspace: WorkspaceRoot,
) -> None:
    """The contract an agent is handed carries the episode path convention.

    Anchored on the path rather than a prose sentence: rewording the section
    is ordinary editing, losing the convention is a regression.
    """
    installed = (workspace / "system/rules/20-memory.md").read_text(encoding="utf-8")
    assert "memory/episodes/YYYY-MM.md" in installed


def test_bootstrap_installs_the_source_grounding_convention(
    workspace: WorkspaceRoot,
) -> None:
    """PROV-01's durable Markdown convention reaches a fresh workspace."""
    installed = (workspace / "system/rules/00-contract.md").read_text(encoding="utf-8")
    assert "## Sources" in installed


def test_force_bootstrap_refreshes_episode_text_into_an_older_workspace(
    workspace: WorkspaceRoot,
) -> None:
    """The live-workspace upgrade path for EPI-01, as a test rather than a procedure."""
    memory_rules = workspace / "system/rules/20-memory.md"
    registry = workspace / "system/projects.yml"
    marker = workspace / "system/meta.yml"
    pointer = workspace / "AGENTS.md"
    memory_rules.write_text("# Memory\n\nContract text predating episodes.\n", encoding="utf-8")
    registry.write_text("projects:\n  durable: {}\n", encoding="utf-8")
    marker.write_text(f'format: {SUPPORTED_FORMAT}\ncreated: "2000-01-01"\n', encoding="utf-8")
    pointer.write_text("local operator guidance\n", encoding="utf-8")

    bootstrap(workspace, force=True)

    assert "memory/episodes/YYYY-MM.md" in memory_rules.read_text(encoding="utf-8")
    assert registry.read_text(encoding="utf-8") == "projects:\n  durable: {}\n"
    assert marker.read_text(encoding="utf-8") == (
        f'format: {SUPPORTED_FORMAT}\ncreated: "2000-01-01"\n'
    )
    assert pointer.read_text(encoding="utf-8") == "local operator guidance\n"


def test_force_bootstrap_refreshes_source_grounding_without_resetting_identity(
    workspace: WorkspaceRoot,
) -> None:
    """The PROV-01 contract refresh preserves owner-controlled workspace state."""
    contract = workspace / "system/rules/00-contract.md"
    registry = workspace / "system/projects.yml"
    marker = workspace / "system/meta.yml"
    pointer = workspace / "AGENTS.md"
    contract.write_text("# Contract predating source grounding\n", encoding="utf-8")
    registry.write_text("projects:\n  durable: {}\n", encoding="utf-8")
    marker.write_text(f'format: {SUPPORTED_FORMAT}\ncreated: "2000-01-01"\n', encoding="utf-8")
    pointer.write_text("local operator guidance\n", encoding="utf-8")

    bootstrap(workspace, force=True)

    assert "## Sources" in contract.read_text(encoding="utf-8")
    assert registry.read_text(encoding="utf-8") == "projects:\n  durable: {}\n"
    assert marker.read_text(encoding="utf-8") == (
        f'format: {SUPPORTED_FORMAT}\ncreated: "2000-01-01"\n'
    )
    assert pointer.read_text(encoding="utf-8") == "local operator guidance\n"


def test_this_checkouts_workspace_serves_the_current_contract() -> None:
    """A live workspace's installed contract must match the source it came from.

    ``get_context`` serves ``workspace/system/rules/*.md`` — *installed copies*.
    Editing the source under ``product/contract/`` changes nothing a running
    agent sees until ``bootstrap_workspace.py --force`` is run, and nothing
    else reconciles the two. That is a silent failure with real consequences:
    two instances were found on 2026-08-08, one of them months old. The
    installed bootstrap prompt predated the ``/compact`` paragraph, so live
    chats were never told compacts existed; and the memory rules named the
    episode convention without naming ``record_episode``, which would have
    sent an agent to ``create_document`` and produced a second ledger for the
    same month at a slugified path.

    The bootstrap tests above prove installation works into a fresh
    ``tmp_path``. They pass while the real workspace is arbitrarily stale,
    because they never look at it. This one does.

    Skips when this checkout has no workspace (CI, fresh clones): the
    directory is git-ignored live user data, so its absence is normal.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    workspace_root = repo_root / "workspace"
    if not (workspace_root / "system" / "meta.yml").is_file():
        pytest.skip("no live workspace in this checkout")

    stale: list[str] = []
    for source_rel, dest_rel in CONTRACT_INSTALLS.items():
        source = repo_root / "product" / "contract" / source_rel
        installed = workspace_root / dest_rel
        if not installed.is_file():
            stale.append(f"{dest_rel} (never installed)")
        elif installed.read_text(encoding="utf-8") != source.read_text(encoding="utf-8"):
            stale.append(f"{dest_rel} (differs from product/contract/{source_rel})")

    assert not stale, (
        "This checkout's workspace is serving stale contract text to every agent:\n  "
        + "\n  ".join(stale)
        + "\n\nRun: uv run python scripts/bootstrap_workspace.py --workspace workspace --force"
        "\n(--force preserves the registry, the format marker, workspace AGENTS.md, and any "
        "rules file not in CONTRACT_INSTALLS.)"
    )


def test_episode_path_convention_agrees_across_contract_and_product() -> None:
    """The convention is stated in one spelling everywhere it is stated at all."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    for relative in (
        "product/contract/rules/20-memory.md",
        "product/00-what-is-ferumind.md",
        "product/spec-flows.md",
    ):
        text = (repo_root / relative).read_text(encoding="utf-8")
        assert "memory/episodes/YYYY-MM.md" in text, relative


@pytest.mark.parametrize("format_version", [SUPPORTED_FORMAT - 1, SUPPORTED_FORMAT + 1])
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
