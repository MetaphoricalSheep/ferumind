#!/usr/bin/env python3
"""Fail closed when the tracked public tree or workflow action pins are unsafe."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent

ALLOWED_WORKSPACE_PATHS = frozenset({"workspace/.gitkeep"})
GENERATED_AGENT_PREFIXES = (
    ".claude/",
    ".codex/",
    ".cursor/",
    ".github/instructions/",
    ".github/prompts/",
    ".github/chatmodes/",
)
GENERATED_AGENT_FILES = frozenset(
    {
        ".github/copilot-instructions.md",
        "CLAUDE.md",
        "GEMINI.md",
    }
)
SENSITIVE_SUFFIXES = frozenset(
    {
        ".age",
        ".db",
        ".jks",
        ".key",
        ".keystore",
        ".netrc",
        ".npmrc",
        ".p12",
        ".pfx",
        ".pem",
        ".pypirc",
        ".secret",
        ".sqlite",
        ".sqlite3",
    }
)
ACTION_LINE = re.compile(
    r"""^\s*(?:-\s*)?["']?uses["']?\s*:\s*"""
    r"(?P<reference>[^\s#]+)(?:\s+#\s*(?P<label>.+))?$"
)
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
VERSION_LABEL = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
DOCKER_DIGEST_REFERENCE = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")


def tracked_paths(repo_root: Path) -> tuple[str, ...]:
    """Return every Git-tracked path using unambiguous NUL separation."""
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is required for public-tree checks")
    result = subprocess.run(  # noqa: S603 - fixed read-only Git command
        [git, "ls-files", "-z"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return tuple(path.decode("utf-8") for path in result.stdout.split(b"\0") if path)


def forbidden_public_path_reason(path: str) -> str | None:
    """Return why *path* must not be public, or ``None`` when allowed."""
    normalized = PurePosixPath(path).as_posix()
    if normalized.startswith("workspace/") and normalized not in ALLOWED_WORKSPACE_PATHS:
        return "live workspace content"
    if normalized in GENERATED_AGENT_FILES or normalized.startswith(GENERATED_AGENT_PREFIXES):
        return "generated agent configuration"

    parts = PurePosixPath(normalized).parts
    if "node_modules" in parts:
        return "installed Node dependency"
    if any(part in {".venv", "htmlcov", "__pycache__"} for part in parts):
        return "generated build or test output"

    name = PurePosixPath(normalized).name
    lowered = name.lower()
    if lowered == ".env" or (lowered.startswith(".env.") and lowered != ".env.example"):
        return "environment file"
    if lowered.startswith(("id_rsa", "id_ed25519", "secrets.")):
        return "credential or secret file"
    if any(lowered.endswith(suffix) for suffix in SENSITIVE_SUFFIXES):
        return "credential, key, or runtime database"
    return None


def forbidden_tracked_paths(repo_root: Path) -> tuple[str, ...]:
    """Return formatted violations for unsafe tracked paths."""
    violations: list[str] = []
    for path in tracked_paths(repo_root):
        reason = forbidden_public_path_reason(path)
        if reason is not None:
            violations.append(f"{path}: {reason}")
    return tuple(violations)


def action_pin_violations(repo_root: Path) -> tuple[str, ...]:
    """Require every external workflow action to use a labeled full commit SHA."""
    workflows = repo_root / ".github" / "workflows"
    if not workflows.is_dir():
        return (".github/workflows: directory is missing",)

    violations: list[str] = []
    workflow_files = sorted((*workflows.glob("*.yml"), *workflows.glob("*.yaml")))
    if not workflow_files:
        return (".github/workflows: no workflow files found",)

    for workflow in workflow_files:
        for line_number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            match = ACTION_LINE.match(line)
            if match is None:
                continue
            reference = match.group("reference").strip("\"'")
            if reference.startswith("./"):
                continue
            if reference.startswith("docker://"):
                if DOCKER_DIGEST_REFERENCE.fullmatch(reference) is None:
                    violations.append(
                        f"{workflow}:{line_number}: Docker action is not pinned to a full sha256 digest"
                    )
                continue
            if "@" not in reference:
                violations.append(f"{workflow}:{line_number}: action reference has no @ revision")
                continue
            _, revision = reference.rsplit("@", 1)
            if COMMIT_SHA.fullmatch(revision) is None:
                violations.append(
                    f"{workflow}:{line_number}: external action is not pinned to a full commit SHA"
                )
            label = (match.group("label") or "").strip()
            if VERSION_LABEL.fullmatch(label) is None:
                violations.append(
                    f"{workflow}:{line_number}: action pin lacks an exact release-version comment"
                )
    return tuple(violations)


def run_checks(repo_root: Path) -> tuple[str, ...]:
    """Return all public-release policy violations."""
    return (*forbidden_tracked_paths(repo_root), *action_pin_violations(repo_root))


def main() -> int:
    violations = run_checks(REPO_ROOT)
    if violations:
        print("Public-tree release checks failed:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 1
    print("Public-tree and GitHub Action pin checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
