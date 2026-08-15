"""Regressions for the first-user onboarding path (REL-008).

Each test here covers something a new user hits before they have any working
setup to fall back on, so a silent regression is expensive.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap_workspace.py"


def _run(
    *args: str,
    env_extra: dict[str, str] | None = None,
    cwd: Path = REPO_ROOT,
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), **(env_extra or {})}
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=180,
    )


class TestBootstrapWorkspaceResolution:
    """``bootstrap_workspace.py`` must resolve the workspace the same way the
    CLI and MCP server do. It used to hardcode the repo default, so exporting
    FERUMIND_WORKSPACE and running bootstrap silently initialized the *repo's*
    workspace instead of the user's.
    """

    def test_env_var_is_honored(self, tmp_path: Path) -> None:
        target = tmp_path / "from-env"
        result = _run(str(BOOTSTRAP), env_extra={"FERUMIND_WORKSPACE": str(target)})
        assert result.returncode == 0, result.stderr
        assert (target / "system" / "meta.yml").is_file()

    def test_explicit_flag_beats_the_env_var(self, tmp_path: Path) -> None:
        from_env = tmp_path / "from-env"
        from_flag = tmp_path / "from-flag"
        result = _run(
            str(BOOTSTRAP),
            "--workspace",
            str(from_flag),
            env_extra={"FERUMIND_WORKSPACE": str(from_env)},
        )
        assert result.returncode == 0, result.stderr
        assert (from_flag / "system" / "meta.yml").is_file()
        assert not from_env.exists(), "the env var must not win over an explicit flag"

    def test_relative_value_resolves_against_the_repo_not_the_cwd(self, tmp_path: Path) -> None:
        """A relative FERUMIND_WORKSPACE must mean the same directory to
        bootstrap and to ``ferumind info``, whatever the caller's cwd.

        The relative value points at *tmp_path* expressed from the repo root,
        and the subprocess runs from a different directory. That keeps the
        assertion honest without the earlier version's cost: passing the bare
        value ``workspace`` bootstrapped the checkout's own ``workspace/`` —
        the owner's live data on a developer machine.
        """
        target = tmp_path / "resolved-workspace"
        caller_cwd = tmp_path / "caller" / "cwd"
        caller_cwd.mkdir(parents=True)
        relative = os.path.relpath(target, REPO_ROOT)
        assert not Path(relative).is_absolute()

        result = _run(
            str(BOOTSTRAP),
            env_extra={"FERUMIND_WORKSPACE": relative},
            cwd=caller_cwd,
        )

        assert result.returncode == 0, result.stderr
        assert (target / "system" / "meta.yml").is_file()
        resolved_against_cwd = (caller_cwd / relative).resolve()
        if resolved_against_cwd != target:
            assert not resolved_against_cwd.exists(), "the value was resolved against the cwd"


class TestProjectCreateCommand:
    """A first project must not require a configured MCP client."""

    def _bootstrap(self, path: Path) -> None:
        result = _run(str(BOOTSTRAP), "--workspace", str(path))
        assert result.returncode == 0, result.stderr

    def _cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return _run("-c", "from ferumind.cli.main import app; app()", *args)

    def test_creates_a_usable_project(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        self._bootstrap(workspace)
        result = self._cli(
            "project", "create", "notes", "--title", "My Notes", "--workspace", str(workspace)
        )
        assert result.returncode == 0, result.stderr
        assert "notes" in result.stdout
        assert (workspace / "projects" / "notes" / "spine.md").is_file()
        assert (workspace / "projects" / "notes" / "rules" / "00-project.md").is_file()

    def test_duplicate_key_fails_without_a_traceback(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        self._bootstrap(workspace)
        args = ("project", "create", "notes", "--title", "N", "--workspace", str(workspace))
        assert self._cli(*args).returncode == 0
        second = self._cli(*args)
        assert second.returncode == 1
        assert "already exists" in second.stderr
        assert "Traceback" not in second.stderr

    def test_created_project_is_visible_to_project_list(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        self._bootstrap(workspace)
        self._cli("project", "create", "notes", "--title", "N", "--workspace", str(workspace))
        listed = self._cli("project", "list", "--workspace", str(workspace))
        assert listed.returncode == 0
        assert "registry, folder, database" in listed.stdout, (
            "a CLI-created project must register in all three sources, exactly like "
            "one created through the MCP create_project tool"
        )


def test_server_advertises_ferumind_version_not_the_sdk_version() -> None:
    """``serverInfo.version`` is what every client shows in its server list.

    The 1.x SDK forwarded no version to the low-level server, which fell back to
    ``pkg_version("mcp")`` — so Ferumind advertised the MCP SDK's version as its
    own and the number moved on every SDK upgrade. mcp 2.x makes ``version`` a
    public constructor parameter, so the workaround is gone; this asserts the
    value a client actually receives over the protocol.
    """
    import tomllib
    from importlib.metadata import version as pkg_version

    import anyio
    from mcp.client.client import Client

    from ferumind.mcp.server import mcp

    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        declared = tomllib.load(handle)["project"]["version"]

    async def server_info() -> tuple[str, str]:
        async with Client(mcp) as client:
            info = client.server_info
            assert info is not None, "the client received no serverInfo"
            return info.name, info.version

    name, reported = anyio.run(server_info)

    assert name == "Ferumind"
    assert reported == declared, (
        f"serverInfo reports {reported!r} but pyproject declares {declared!r}."
    )
    assert reported != pkg_version("mcp"), "serverInfo is reporting the MCP SDK version again"


@pytest.mark.parametrize("command", ["create", "list", "delete"])
def test_project_subcommands_are_registered(command: str) -> None:
    result = _run("-c", "from ferumind.cli.main import app; app()", "project", "--help")
    assert result.returncode == 0
    assert command in result.stdout


class TestQuickStartDocumentation:
    """The README quick start is the only path a new user has.

    These assert the claims it makes stay true; the launcher's own behaviour is
    covered by tests/unit/test_tunnel_scripts.py.
    """

    def _readme(self) -> str:
        return (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    def test_documented_launcher_exists_and_is_executable(self) -> None:
        launcher = REPO_ROOT / "scripts" / "ferumind-mcp-stdio"
        assert f"scripts/{launcher.name}" in self._readme()
        assert launcher.is_file()
        assert launcher.stat().st_mode & 0o111, "documented launcher is not executable"

    def test_client_config_block_points_at_the_launcher(self) -> None:
        """AGENTS.md requires a quiet executable wrapper, never a shell command.

        Anchored to the JSON block a reader copies rather than to a heading, so
        the README can be restructured without breaking the guard. Only the
        block is checked: the surrounding prose names ``uv run ferumind mcp
        serve`` deliberately, to warn against it.
        """
        readme = self._readme()
        start = readme.index("```json")
        block = readme[start : readme.index("```", start + 7)]
        assert "ferumind-mcp-stdio" in block
        assert "uv run" not in block, (
            "the client config must invoke the wrapper, which loads .env and strips "
            "control-plane credentials, not a raw shell command"
        )

    def test_documented_tool_count_matches_the_server(self) -> None:
        import anyio

        from ferumind.mcp.server import mcp, register_all_tools

        register_all_tools()
        actual = len(anyio.run(mcp.list_tools))
        assert f"{actual}\ntools" in self._readme() or f"{actual} tools" in self._readme(), (
            f"README states a tool count that is not {actual}"
        )

    def test_the_spec_states_the_same_tool_count_as_the_server(self) -> None:
        """The spec restates the total three times, and the README guard misses it.

        SKILL-01 added one tool. Four inventory guards caught it and the README
        guard caught the fifth place — but ``spec-mcp.md`` names the total in
        three separate section summaries, none of which was checked, so the
        locked spec silently disagreed with the server it specifies. Any
        arithmetic in prose drifts; this asserts every occurrence.
        """
        import anyio

        from ferumind.mcp.server import mcp, register_all_tools

        register_all_tools()
        actual = len(anyio.run(mcp.list_tools))
        spec = (REPO_ROOT / "product" / "spec-mcp.md").read_text(encoding="utf-8")

        stated = list(re.finditer(r"=\s*(\d+)\s*tools", spec))
        assert stated, "spec-mcp.md no longer states a tool total"

        stale: list[str] = []
        for match in stated:
            if int(match.group(1)) == actual:
                continue
            # §5.3b states a deliberate running subtotal and then reconciles it
            # in the following sentence. That is allowed; an unreconciled
            # number is not.
            if f"total to {actual}" in spec[match.end() : match.end() + 200]:
                continue
            stale.append(match.group(0))
        assert not stale, f"spec-mcp.md states {stale} but the server registers {actual} tools"

    # Removed: test_quick_start_reaches_first_context. It grepped four keywords
    # between two headings, so it passed on any README containing those strings
    # in any order and pinned the document's structure for no real coverage.
    # QUAL-02's dead end is genuinely covered by TestBootstrapWorkspaceResolution
    # and TestProjectCreateCommand above, which execute the documented commands
    # instead of looking for them.


def _normalized(text: str) -> str:
    """Collapse wrapping so a reflowed document is not a spurious failure."""
    return " ".join(text.split())


class TestSurfaceDocumentation:
    """Two documents restate the live MCP surface in prose.

    Prose has no compiler, so both drifted silently: the spec's "exact"
    ``initialize`` string was missing two sentences the server had been
    shipping, and ``AGENTS.md``'s taxonomy classified 33 of 48 tools while
    presenting itself as complete. Each guard below derives the truth from the
    running server rather than from a second copy of the list.
    """

    def _surface(self) -> dict[str, tuple[bool | None, bool | None]]:
        import anyio

        from ferumind.mcp.server import mcp, register_all_tools

        register_all_tools()
        tools = anyio.run(mcp.list_tools)
        return {
            tool.name: (
                tool.annotations.read_only_hint if tool.annotations else None,
                tool.annotations.idempotent_hint if tool.annotations else None,
            )
            for tool in tools
        }

    def test_spec_states_the_exact_initialize_instructions(self) -> None:
        """``spec-mcp`` §9 calls its blockquote the exact string; hold it to that.

        The envelope and propose-then-apply sentences were added to
        ``INSTRUCTIONS`` and never reached the spec, so the locked document
        under-described what every client receives on connect.
        """
        from ferumind.mcp.server import INSTRUCTIONS

        spec = (REPO_ROOT / "product" / "spec-mcp.md").read_text(encoding="utf-8")
        marker = "Exact string"
        start = spec.index(marker)
        quoted = [line[1:].strip() for line in spec[start:].splitlines() if line.startswith(">")]
        assert quoted, "spec-mcp.md §9 no longer quotes the initialize instructions"
        assert _normalized(" ".join(quoted)) == _normalized(INSTRUCTIONS), (
            "spec-mcp.md §9 and mcp/server.py INSTRUCTIONS disagree. The spec "
            "calls this the exact string, so update the blockquote."
        )

    def test_agents_md_classifies_every_registered_tool(self) -> None:
        """Every tool appears in the taxonomy, under its real annotations.

        ``AGENTS.md`` is what a coding agent reads before touching the MCP
        surface. A tool missing from the taxonomy reads as a tool that does not
        exist, and one filed under the wrong category reads as a promise about
        whether it writes user Markdown.
        """
        categories = {
            "Read-only": (True, True),
            "Staging": (True, False),
            "Content-mutating": (False, False),
        }
        agents_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        section = agents_md[agents_md.index("### Tool annotation taxonomy") :]
        section = section[: section.index("\n## ")]

        # Each category is "**Label** (hints): `a`, `b`, `c`." — the name list
        # runs from the colon after the hints to the first full stop, so the
        # explanatory prose that follows it cannot leak in as a tool name.
        documented: dict[str, tuple[bool, bool]] = {}
        for label, hints in categories.items():
            marker = f"**{label}**"
            assert marker in section, f"AGENTS.md taxonomy lost its {label} category"
            tail = section[section.index(marker) + len(marker) :]
            names = tail[tail.index("):") + 2 : tail.index(".")]
            for name in re.findall(r"`([a-z][a-z0-9_]+)`", names):
                documented.setdefault(name, hints)

        actual = self._surface()
        missing = sorted(set(actual) - set(documented))
        assert not missing, f"AGENTS.md taxonomy omits registered tool(s): {missing}"

        unknown = sorted(set(documented) - set(actual))
        assert not unknown, (
            f"AGENTS.md taxonomy names tool(s) the server does not register: {unknown}"
        )

        misfiled = sorted(name for name, hints in documented.items() if actual[name] != hints)
        assert not misfiled, (
            f"AGENTS.md files {misfiled} under a category whose annotations do not "
            "match what the server advertises"
        )
