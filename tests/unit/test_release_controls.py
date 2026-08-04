"""Regression tests for repository publication and release controls."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml
from check_public_tree import (
    action_pin_violations,
    forbidden_public_path_reason,
    forbidden_tracked_paths,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, raw, _ = text.split("---", 2)
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return cast("dict[str, object]", parsed)


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("workspace/.gitkeep", True),
        ("workspace/projects/private/spine.md", False),
        (".env.example", True),
        (".env", False),
        (".env.production", False),
        ("data/lattice.sqlite", False),
        ("keys/id_ed25519.pub", False),
        (".github/instructions/generated.md", False),
        (".github/workflows/ci.yml", True),
        (".opencode/node_modules/package/index.js", False),
    ],
)
def test_public_path_policy(path: str, allowed: bool) -> None:
    assert (forbidden_public_path_reason(path) is None) is allowed


def test_current_tracked_tree_contains_no_forbidden_public_paths() -> None:
    assert forbidden_tracked_paths(REPO_ROOT) == ()


def test_current_workflow_actions_are_immutably_pinned() -> None:
    assert action_pin_violations(REPO_ROOT) == ()


def test_action_pin_check_rejects_movable_tags(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        'steps:\n  - "uses" : actions/checkout@v6\n',
        encoding="utf-8",
    )

    violations = action_pin_violations(tmp_path)

    assert any("not pinned to a full commit SHA" in violation for violation in violations)
    assert any("lacks an exact release-version comment" in violation for violation in violations)


def test_action_pin_check_requires_docker_image_digests(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    workflow = workflows / "ci.yml"
    workflow.write_text(
        "steps:\n  - uses: docker://alpine:latest\n",
        encoding="utf-8",
    )

    violations = action_pin_violations(tmp_path)

    assert any("not pinned to a full sha256 digest" in violation for violation in violations)

    digest = "a" * 64
    workflow.write_text(
        f"steps:\n  - uses: docker://alpine@sha256:{digest}\n",
        encoding="utf-8",
    )
    assert action_pin_violations(tmp_path) == ()


def test_justfile_does_not_globally_load_tunnel_secrets() -> None:
    justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    assert "dotenv-filename" not in justfile
    assert "dotenv-load" not in justfile


def test_tunnel_just_documentation_does_not_forward_a_literal_separator() -> None:
    instructions = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "just tunnel -- --" not in instructions
    assert "| `just tunnel --init` |" in instructions


def test_publication_docs_do_not_overstate_current_tree_check() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    for document in (readme, security):
        normalized = " ".join(document.split())
        assert "file contents" in normalized
        assert "Git history" in normalized
        assert "necessary" in normalized


def test_test_fixer_agent_permissions_are_machine_readable_and_fail_closed() -> None:
    metadata = _frontmatter(REPO_ROOT / ".opencode" / "agents" / "test-fixer.md")
    assert metadata["mode"] == "subagent"
    assert metadata["model"] == "opencode-go/deepseek-v4-flash-free"

    permission = metadata["permission"]
    assert isinstance(permission, dict)
    permission = cast("dict[str, object]", permission)
    assert permission["task"] == "deny"
    assert permission["webfetch"] == "deny"
    assert permission["websearch"] == "deny"
    assert permission["external_directory"] == "deny"

    bash = permission["bash"]
    assert isinstance(bash, dict)
    bash = cast("dict[str, object]", bash)
    assert next(iter(bash.items())) == ("*", "ask")
    for pattern in ("git push*", "git commit*", "git reset --hard*", "rm -rf *", "sudo *"):
        assert bash[pattern] == "deny"


def test_every_opencode_skill_has_discoverable_metadata() -> None:
    skills_root = REPO_ROOT / ".opencode" / "skills"
    for skill_file in sorted(skills_root.glob("*/SKILL.md")):
        text = skill_file.read_text(encoding="utf-8")
        metadata = _frontmatter(skill_file)
        assert metadata["name"] == skill_file.parent.name
        description = metadata["description"]
        assert isinstance(description, str)
        assert description.startswith("Use ")
        assert "compatibility: " not in str(metadata["compatibility"])
        assert "(future)" not in text
        assert "future versioning" not in text


def test_verifier_and_ci_run_release_checks() -> None:
    verifier = (REPO_ROOT / "scripts" / "verify.sh").read_text(encoding="utf-8")
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/check_public_tree.py" in verifier
    assert "scripts/check_distribution.py" in workflow
