"""Regression tests for repository publication and release controls."""

from __future__ import annotations

import re
import subprocess
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from check_public_tree import (
    action_pin_violations,
    forbidden_public_path_reason,
    forbidden_tracked_paths,
    tracked_paths,
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
        ("data/ferumind.sqlite", False),
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
    """``just tunnel`` takes its flags directly; a ``--`` separator reaches the script.

    Anchored to the command text rather than to the table row that currently
    carries it, so the recipe table can be restructured without a false failure.
    """
    instructions = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "just tunnel -- --" not in instructions
    assert "`just tunnel --init`" in instructions


def test_publication_docs_do_not_overstate_current_tree_check() -> None:
    """Both documents must qualify the public-tree check in the same sentence.

    Previously three keywords anywhere in the file satisfied this, so a
    rewrite that dropped the qualification entirely could still pass as long
    as "necessary" survived elsewhere. The claim is one sentence; assert that
    sentence.
    """
    for name in ("README.md", "SECURITY.md"):
        normalized = " ".join((REPO_ROOT / name).read_text(encoding="utf-8").split())
        sentences = [s for s in normalized.split(". ") if "public-tree" in s]
        assert sentences, f"{name} no longer describes the public-tree check"
        claim = sentences[0]
        limits = f"{name} must say the check inspects neither file contents nor Git history, in the sentence that introduces it. Got: {claim!r}"
        assert "file contents" in claim, limits
        assert "Git history" in claim, limits

        qualified = (
            f"{name} must not present the public-tree check as approval to publish. Got: {claim!r}"
        )
        assert "not" in claim, qualified
        assert "necessary" in claim or "sufficient" in claim, qualified


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


def _ci_jobs() -> dict[str, Any]:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    return cast("dict[str, Any]", cast("dict[str, Any]", workflow)["jobs"])


def test_ci_gate_requires_every_other_job() -> None:
    """A job branch protection does not reach is a job that cannot block a merge.

    ``ci-gate`` is the only required status check, so a new job that never
    reaches its ``needs`` list runs, reports, and is ignored — green merges
    while it fails. That is silent by construction, which is why it is pinned
    here rather than left to review.
    """
    jobs = _ci_jobs()
    required = cast("list[str]", jobs["ci-gate"]["needs"])
    missing = sorted(set(jobs) - {"ci-gate"} - set(required))
    assert not missing, (
        f"ci.yml jobs {missing} are not in ci-gate's needs, so branch protection "
        "ignores them. Add them to needs and to the echo block that reports results."
    )


def test_secret_scan_covers_all_history_without_leaking_findings() -> None:
    """The three flags that decide whether the scan proves anything.

    A shallow checkout makes ``--log-opts=--all`` scan one commit; a missing
    ``--redact`` prints a real finding into an Actions log that goes public
    with the repository. Both fail green, so neither is left to review.
    """
    secret_scan = _ci_jobs()["secret-scan"]
    steps = cast("list[dict[str, Any]]", secret_scan["steps"])
    checkout = next(
        step for step in steps if str(step.get("uses", "")).startswith("actions/checkout")
    )
    assert checkout["with"]["fetch-depth"] == 0, "secret-scan must check out full history"

    scan = "\n".join(str(step.get("run", "")) for step in steps)
    assert "--log-opts='--all'" in scan, "secret-scan must cover every ref, not just HEAD"
    assert "--redact" in scan, "secret-scan must not echo findings into a public log"
    assert "sha256sum --check" in scan, "the gitleaks download must be checksum-verified"


def test_gitleaks_allowlists_are_conjunctive() -> None:
    """``condition = "AND"`` is the difference between a scoped waiver and a hole.

    Gitleaks defaults an allowlist's criteria to OR, so a ``paths`` plus
    ``regexes`` entry silences every match in that path *and* every match of
    that regex repository-wide. The config reads identically either way.
    """
    config = tomllib.loads((REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    allowlists = cast("list[dict[str, Any]]", config.get("allowlists", []))
    for allowlist in allowlists:
        criteria = {"paths", "regexes", "commits", "stopwords"} & set(allowlist)
        if len(criteria) > 1:
            assert allowlist.get("condition") == "AND", (
                f"allowlist {allowlist.get('description')!r} combines {sorted(criteria)} "
                'without condition = "AND", so gitleaks ORs them into a wider waiver '
                "than intended."
            )


# ── Python support range ────────────────────────────────────────────────────
#
# Six places declare a Python version and every one of them can drift
# independently. These tests are the single enforcement point: change
# ``requires-python`` without touching CI (or vice versa) and the suite fails
# with the exact fix. See docs/python-support.md for the policy.


def _pyproject() -> dict[str, object]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _supported_minors() -> list[int]:
    """The 3.x minors allowed by ``requires-python``, as an explicit list.

    Requires both a lower and an upper bound. An open-ended ``>=3.12`` would
    silently claim support for every future Python, which is precisely the
    drift REL-007 closed.
    """
    project = cast("dict[str, object]", _pyproject()["project"])
    spec = cast("str", project["requires-python"])
    lower: int | None = None
    upper: int | None = None
    for clause in (part.strip() for part in spec.split(",")):
        if clause.startswith(">="):
            lower = int(clause.removeprefix(">=").split(".")[1])
        elif clause.startswith("<"):
            upper = int(clause.removeprefix("<").split(".")[1])
    assert lower is not None, f"requires-python {spec!r} needs a >= lower bound"
    assert upper is not None, (
        f"requires-python {spec!r} needs a < upper bound. An unbounded range claims "
        "support for Python versions that have never been tested."
    )
    return list(range(lower, upper))


def _ci_matrix_minors() -> list[int]:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    jobs = cast("dict[str, Any]", workflow["jobs"])
    versions = cast("list[str]", jobs["verify"]["strategy"]["matrix"]["python-version"])
    return [int(version.split(".")[1]) for version in versions]


def test_supported_python_range_matches_ci() -> None:
    """Every interpreter the package accepts is actually exercised."""
    supported = _supported_minors()
    tested = _ci_matrix_minors()
    assert sorted(tested) == sorted(supported), (
        f"requires-python allows 3.{supported} but CI tests 3.{tested}. "
        "Add the missing minor to the ci.yml matrix, or narrow requires-python. "
        "Never advertise an interpreter no job runs."
    )


def test_supported_python_range_matches_classifiers() -> None:
    project = cast("dict[str, object]", _pyproject()["project"])
    classifiers = cast("list[str]", project["classifiers"])
    prefix = "Programming Language :: Python :: 3."
    declared = sorted(
        int(item.removeprefix(prefix)) for item in classifiers if item.startswith(prefix)
    )
    assert declared == sorted(_supported_minors()), (
        f"classifiers declare 3.{declared} but requires-python allows "
        f"3.{sorted(_supported_minors())}."
    )


def test_linters_target_the_oldest_supported_python() -> None:
    """Ruff and Pyright must target the floor, not the newest interpreter.

    Targeting the newest would let 3.14-only syntax or stdlib pass review and
    then fail at runtime on 3.12 — which CI would catch only after the fact,
    and only if that code path happened to be covered.
    """
    data = _pyproject()
    tool = cast("dict[str, Any]", data["tool"])
    floor = min(_supported_minors())
    assert tool["ruff"]["target-version"] == f"py3{floor}", (
        f"ruff target-version must be py3{floor} (the oldest supported minor)."
    )
    assert tool["pyright"]["pythonVersion"] == f"3.{floor}", (
        f"pyright pythonVersion must be 3.{floor} (the oldest supported minor)."
    )


def test_readme_states_the_real_supported_range() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    minors = _supported_minors()
    expected = f"3.{min(minors)}-3.{max(minors)}"
    assert expected in readme, (
        f"README must state the supported range as {expected!r}. An open "
        '"3.12+" implies support for untested future interpreters.'
    )


#: An open-ended Python claim: "3.12+", "3.12 or newer", "3.12 and above".
#: The README guard above only watches one file, so CONTRIBUTING.md carried
#: "Python 3.12 or newer" — a support claim for interpreters no job runs —
#: from the day the upper bound was introduced until REL-037 found it.
_OPEN_ENDED_PYTHON = re.compile(
    r"[Pp]ython\s+3\.\d+\s*(?:\+|or newer|or later|or above|and above|and newer)"
)


def test_no_tracked_document_claims_an_open_ended_python_range() -> None:
    """``requires-python`` has an upper bound; prose must not contradict it.

    Any tracked Markdown may restate the range. Rather than listing the files
    that are allowed to, this refuses the shape that is always wrong: a floor
    with no ceiling.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "*.md"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    ).stdout.split()

    offenders: list[str] = []
    for relative in tracked:
        if relative.startswith(("workspace/", "tests/fixtures/")):
            continue
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        for match in _OPEN_ENDED_PYTHON.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{relative}:{line}: {match.group(0)!r}")

    minors = _supported_minors()
    assert not offenders, (
        "These documents claim support for untested future interpreters. State "
        f"the range as 3.{min(minors)}-3.{max(minors)}, or defer to "
        "docs/python-support.md:\n  " + "\n  ".join(offenders)
    )


# ── Bare version labels ─────────────────────────────────────────────────────
#
# Three numbers version this project and they never line up: the workspace
# ``format`` (3), the DB ``schema`` / ``PRAGMA user_version`` (3), and the
# package semver (0.1.0). A bare "v2" belongs to none of them, so nothing
# proves it wrong when it goes stale — which is how the repository ended up
# announcing itself as its own second major version at package 0.1.0.
# Name the axis instead. See product/spec-versioning.md §0.1.

#: A bare label: ``v`` + digits that is neither part of a larger token nor a
#: dotted version. Deliberately blind to ``garden-v2`` (an identifier),
#: ``v1.txt`` (a filename), ``checkout@v7`` (an action pin), and ``v0.2.0``
#: (a git release tag) — those carry an axis already.
_BARE_VERSION_LABEL = re.compile(r"(?<![\w.@/-])[vV](\d+)(?![\d.])")

#: External dependencies whose own major version is legitimately a bare
#: label. Ferumind's axes never appear here.
_EXTERNAL_VERSION_LABEL = re.compile(r"Pydantic\s+v\d+")

_PROSE_SUFFIXES = frozenset({".md", ".py", ".sql", ".toml", ".yml", ".yaml"})

#: This module states the counterexamples the rule forbids, so it cannot be
#: subject to its own rule. It is the only exemption, and it is asserted
#: below rather than hidden inside the collector.
_GUARD_DEFINITION = "tests/unit/test_release_controls.py"


def bare_version_labels(text: str) -> tuple[str, ...]:
    """Return the axis-less version labels in *text*."""
    return tuple(
        match.group(0)
        for match in _BARE_VERSION_LABEL.finditer(_EXTERNAL_VERSION_LABEL.sub("", text))
    )


def bare_version_label_violations(root: Path, paths: Iterable[str]) -> tuple[str, ...]:
    """Return ``path:line: label`` for every bare label in the given files."""
    violations: list[str] = []
    for relative in paths:
        path = root / relative
        if path.suffix.lower() not in _PROSE_SUFFIXES or not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            violations.extend(
                f"{relative}:{number}: bare version label {label!r}"
                for label in bare_version_labels(line)
            )
    return tuple(violations)


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("# Spec: MCP Server v2", True),
        ("the v1 implementation", True),
        ("Frontmatter V2 keys", True),
        ("the format 2 layout", False),
        ("schema 2 / PRAGMA user_version", False),
        ("git tag v0.2.0", False),
        ("project key garden-v2", False),
        ("library/v1.txt", False),
        ("uses: actions/checkout@v7", False),
        ("Use Pydantic v2 for config", False),
    ],
)
def test_bare_version_label_detection(text: str, flagged: bool) -> None:
    assert bool(bare_version_labels(text)) is flagged


def test_tracked_prose_carries_no_bare_version_labels() -> None:
    tracked = tracked_paths(REPO_ROOT)
    assert _GUARD_DEFINITION in tracked, (
        f"{_GUARD_DEFINITION} is exempt but no longer tracked under that name; "
        "a renamed guard would exempt nothing and flag its own counterexamples."
    )
    scanned = tuple(path for path in tracked if path != _GUARD_DEFINITION)
    assert bare_version_label_violations(REPO_ROOT, scanned) == ()


def test_bare_version_label_guard_catches_injected_drift(tmp_path: Path) -> None:
    (tmp_path / "spec.md").write_text("# Spec: MCP Server v2\n", encoding="utf-8")
    (tmp_path / "core.py").write_text('"""Parsing for the v2 layout."""\n', encoding="utf-8")
    (tmp_path / "notes.rst").write_text("v2 everywhere\n", encoding="utf-8")

    violations = bare_version_label_violations(tmp_path, ("spec.md", "core.py", "notes.rst"))

    assert violations == (
        "spec.md:1: bare version label 'v2'",
        "core.py:1: bare version label 'v2'",
    )


# ── Versioning and release scheme ───────────────────────────────────────────
#
# The scheme is stated in four places — pyproject.toml, CHANGELOG.md,
# README.md, and AGENTS.md — because each has a different reader. Four copies
# of a rule drift, so the ones that can be compared mechanically are.
# docs/releases.md is the human source of truth. See REL-038.

#: ``0.MINOR.PATCH``. Going to 1.0.0 means deleting this guard, which is the
#: point: it is a promise about backports, deprecation, and support windows
#: that REL-003 has parked, not a number bump.
_PRE_ONE_ZERO = re.compile(r"^0\.\d+\.\d+$")

#: ``## [0.1.0] - 2026-08-15``. Keep a Changelog's released-section heading.
_CHANGELOG_RELEASE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$", re.MULTILINE)


def _project_version() -> str:
    project = cast("dict[str, object]", _pyproject()["project"])
    return cast("str", project["version"])


def test_package_version_is_pre_one_zero() -> None:
    version = _project_version()
    assert _PRE_ONE_ZERO.match(version), (
        f"version {version!r} must be 0.MINOR.PATCH. Shipping 1.0.0 makes the "
        "semantic-version promise REL-003 parked; see docs/releases.md."
    )


def test_changelog_newest_release_matches_the_package_version() -> None:
    """A tag, a version, and a changelog entry are one release or a lie."""
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    releases = _CHANGELOG_RELEASE.findall(changelog)
    assert releases, "CHANGELOG.md carries no released section"
    assert releases[0] == _project_version(), (
        f"CHANGELOG.md's newest release is {releases[0]!r} but pyproject.toml "
        f"declares {_project_version()!r}. Cutting a release moves both."
    )


def test_changelog_has_an_unreleased_section() -> None:
    """Pull requests write here; without it they have nowhere to land."""
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [Unreleased]" in changelog


def test_versioning_docs_agree_that_the_python_api_is_unversioned() -> None:
    """The claim most likely to drift into an accidental promise.

    Everything else Ferumind versions is a surface a user touches deliberately.
    ``import ferumind`` is the one that looks like a public API, is not one,
    and would silently become a compatibility obligation if a document forgot
    to say so.
    """
    for name in ("README.md", "docs/releases.md", "AGENTS.md"):
        # Normalized: the phrase wraps across lines in every one of them.
        text = " ".join((REPO_ROOT / name).read_text(encoding="utf-8").split())
        assert "import API is private" in text, (
            f"{name} must state that the Python import API is private and "
            "unversioned. Without it the version number reads as covering it."
        )


def test_readme_versioning_states_what_it_does_not_promise() -> None:
    """One promise, and an explicit list of the things it is not.

    Asserts the disclaimer rather than hunting for promise-shaped phrasing:
    a rewrite that drops the limits is the failure worth catching, and a
    keyword blocklist cannot tell a promise from its own negation.
    """
    readme = " ".join((REPO_ROOT / "README.md").read_text(encoding="utf-8").split())
    assert "does not promise" in readme, "README must state the limits of the version promise"
    for limit in ("backports", "deprecation window"):
        assert limit in readme, f"README must name {limit!r} among what is not promised"
