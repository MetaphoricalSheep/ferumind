"""The generated agent configs have to be loadable, not merely written.

``.cursor/``, ``.claude/``, ``.codex/``, and ``.github/`` are generated and
git-ignored, so nothing reviews their contents and no human opens them. That
is exactly how a generator can emit files every runtime silently ignores and
nobody notices: the sync script reported success, the files existed, and
Claude Code loaded none of the agents and every skill with ``<!--`` as its
description.

So the load-bearing test here is mechanical and total: generate the whole tree
into ``tmp_path`` from the real committed sources, and assert that every file
carrying frontmatter carries it where a parser will find it — line one,
column one.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from sync_agent_configs import (
    GENERATED_HEADER,
    banner,
    claude_agent_document,
    claude_agent_tools,
    frontmatter_fields,
    skill_section,
    split_frontmatter,
    strip_frontmatter,
    sync_claude,
    sync_codex,
    sync_copilot,
    sync_cursor,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The OpenCode test-fixer's shape, trimmed to what translation must handle:
#: a nested pattern map, an explicit denial, and keys no other runtime knows.
OPENCODE_AGENT = """---
description: Runs verification and repairs safe failures.
mode: subagent
model: opencode-go/deepseek-v4-flash-free
temperature: 0.1
steps: 60
permission:
  read: allow
  edit: allow
  task: deny
  webfetch: deny
  bash:
    "*": ask
    "git push*": deny
---

Use the test-fix skill.
"""


@pytest.fixture
def generated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run every target's sync against the real sources, into ``tmp_path``.

    The committed ``.opencode/`` tree and ``AGENTS.md`` are the inputs a real
    run uses; only the destination moves, so this exercises the generator
    rather than a fixture-shaped imitation of it.
    """
    import sync_agent_configs

    shutil.copytree(REPO_ROOT / ".opencode", tmp_path / ".opencode")
    shutil.copy(REPO_ROOT / "AGENTS.md", tmp_path / "AGENTS.md")
    monkeypatch.setattr(sync_agent_configs, "REPO_ROOT", tmp_path)

    agents = sync_agent_configs.read_agents_md()
    skills = sync_agent_configs.read_opencode_skills()
    opencode_agents = sync_agent_configs.read_opencode_agents()
    sync_cursor(agents, skills, opencode_agents, force=True)
    sync_claude(agents, skills, opencode_agents, force=True)
    sync_copilot(agents, skills, opencode_agents, force=True)
    sync_codex(skills, opencode_agents, force=True)
    return tmp_path


def _hides_frontmatter(content: str) -> bool:
    """Return whether *content* has frontmatter a parser will never reach."""
    if content.startswith("---\n"):
        return False
    return content.replace(GENERATED_HEADER, "", 1).startswith("---\n")


def _generated_documents(root: Path) -> list[Path]:
    found = [
        path
        for directory in (".cursor", ".claude", ".codex", ".github")
        for path in sorted((root / directory).rglob("*"))
        if path.is_file()
    ]
    assert found, "the sync produced nothing to check"
    return found


class TestEveryGeneratedFileLoads:
    def test_no_document_hides_its_frontmatter_behind_the_banner(self, generated: Path) -> None:
        """The whole bug, asserted directly: frontmatter must be first.

        A file whose frontmatter only appears once the banner is removed is a
        file the runtime reads and gets nothing from.
        """
        offenders = [
            path.relative_to(generated).as_posix()
            for path in _generated_documents(generated)
            if _hides_frontmatter(path.read_text(encoding="utf-8"))
        ]
        assert not offenders

    def test_that_check_catches_what_the_generator_used_to_emit(self) -> None:
        """Guard the guard: the old output must still be recognized as broken."""
        assert _hides_frontmatter(f"{GENERATED_HEADER}---\nname: x\n---\n\nbody\n")
        assert not _hides_frontmatter(f"---\nname: x\n---\n{GENERATED_HEADER}\nbody\n")
        assert not _hides_frontmatter(f"{GENERATED_HEADER}# Just prose\n")

    def test_frontmatter_parses_wherever_it_appears(self, generated: Path) -> None:
        for path in _generated_documents(generated):
            content = path.read_text(encoding="utf-8")
            if not content.startswith("---\n"):
                continue
            raw = split_frontmatter(content)[0]
            assert raw, path
            loaded = yaml.safe_load(raw.strip("-\n"))
            assert isinstance(loaded, dict), path

    def test_every_document_records_where_it_came_from(self, generated: Path) -> None:
        for path in _generated_documents(generated):
            assert GENERATED_HEADER.strip() in path.read_text(encoding="utf-8"), path


class TestClaudeAgents:
    def test_agents_declare_what_claude_code_requires(self, generated: Path) -> None:
        agents = sorted((generated / ".claude" / "agents").glob("*.md"))
        assert agents, "no Claude agent was generated"
        for path in agents:
            fields = frontmatter_fields(path.read_text(encoding="utf-8"))
            assert fields.get("name") == path.stem, path
            assert isinstance(fields.get("description"), str), path
            assert fields["description"], path

    def test_opencode_only_keys_do_not_survive(self, generated: Path) -> None:
        """``mode``/``steps``/``temperature``/``permission`` mean nothing here."""
        for path in sorted((generated / ".claude" / "agents").glob("*.md")):
            fields = frontmatter_fields(path.read_text(encoding="utf-8"))
            assert not {"mode", "steps", "temperature", "permission"} & set(fields), path

    def test_the_name_comes_from_the_filename(self) -> None:
        document = claude_agent_document("test-fixer", OPENCODE_AGENT)
        assert document.startswith("---\n")
        assert frontmatter_fields(document)["name"] == "test-fixer"

    def test_the_body_is_carried_through_untouched(self) -> None:
        document = claude_agent_document("test-fixer", OPENCODE_AGENT)
        assert "Use the test-fix skill." in document

    def test_an_unmapped_model_falls_back_rather_than_leaking(self) -> None:
        """An OpenCode model name would be unresolvable, so it never survives."""
        document = claude_agent_document("test-fixer", OPENCODE_AGENT)
        assert frontmatter_fields(document)["model"] == "sonnet"

    def test_a_source_without_frontmatter_still_produces_a_loadable_agent(self) -> None:
        document = claude_agent_document("bare", "Just a body.\n")
        fields = frontmatter_fields(document)
        assert fields["name"] == "bare"
        assert isinstance(fields["description"], str)
        assert fields["description"]


class TestPermissionTranslation:
    def test_denied_capabilities_do_not_become_tools(self) -> None:
        tools = claude_agent_tools(frontmatter_fields(OPENCODE_AGENT)["permission"])
        assert "Agent" not in tools, "task: deny must not yield the Agent tool"
        assert "WebFetch" not in tools

    def test_allowed_capabilities_do(self) -> None:
        tools = claude_agent_tools(frontmatter_fields(OPENCODE_AGENT)["permission"])
        assert {"Read", "Edit", "Write", "Bash"} <= set(tools)

    def test_a_pattern_map_grants_the_tool_when_anything_is_permitted(self) -> None:
        """Claude Code has no per-command granularity: it is the tool or none."""
        assert claude_agent_tools({"bash": {"*": "ask", "git push*": "deny"}}) == ["Bash"]

    def test_a_wholly_denied_pattern_map_grants_nothing(self) -> None:
        assert claude_agent_tools({"bash": {"*": "deny"}}) == []

    def test_an_unknown_permission_key_is_dropped_rather_than_guessed(self) -> None:
        assert claude_agent_tools({"doom_loop": "allow"}) == []


class TestDocumentHelpers:
    def test_the_banner_lands_below_existing_frontmatter(self) -> None:
        result = banner("---\nname: x\n---\nbody\n")
        assert result.startswith("---\nname: x\n---\n")
        assert GENERATED_HEADER.strip() in result

    def test_the_banner_leads_a_document_without_frontmatter(self) -> None:
        assert banner("# Heading\n").startswith("<!--")

    def test_a_horizontal_rule_is_not_mistaken_for_frontmatter(self) -> None:
        document = "# Heading\n\n---\n\nmore\n"
        assert split_frontmatter(document) == ("", document)

    def test_an_unterminated_block_is_left_alone(self) -> None:
        document = "---\nname: x\nno closing marker\n"
        assert split_frontmatter(document) == ("", document)

    def test_stripping_leaves_the_body(self) -> None:
        assert strip_frontmatter("---\nname: x\n---\n\nbody\n") == "body\n"

    def test_an_embedded_skill_keeps_its_description_as_prose(self) -> None:
        section = skill_section("demo", "---\ndescription: Do the thing.\n---\n\n# Demo\n")
        assert not section.startswith("---")
        assert "Do the thing." in section
        assert "# Demo" in section
