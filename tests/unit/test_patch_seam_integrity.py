"""Guard the monkeypatch seams that the ``core/writes.py`` extraction will move.

``monkeypatch.setattr(module, "name", fake)`` only does anything if the code
under test reaches ``name`` *through that module*. Dozens of tests patch
attributes that started life on ``ferumind.core.writes`` — ``save_registry``,
``fetch_remote_file``, ``record_operation``, and a dozen ``MAX_*`` limits — and
REL-024 to REL-027 move the code behind every one of them into a domain module.

That makes for a trap with no natural alarm. When a symbol moves out and
``writes`` keeps it as a compatibility re-export, ``monkeypatch.setattr``
**still succeeds**: the attribute is there to be replaced. But the module that
now runs the code holds its own reference, so the fake is never consulted. The
patch becomes a no-op, and a test written to prove a failure path keeps passing
while proving nothing. A suite in that state does not merely lose coverage — it
actively lies to the next extraction ticket.

Two independent checks close that off.

:class:`TestPatchedSymbolsAreLive` asserts the structural precondition: a
patched attribute must be reached either as a global inside the patched module,
or as ``alias.attribute`` from some other first-party module. A dead re-export
satisfies neither and fails here. (A symbol that vanished outright fails louder
still, inside ``monkeypatch``.) The second form is the one that survives
extraction best — ``upload_staging.remove_staging_dir(...)`` resolves at call
time — which is why the extraction map recommends it for the moved modules.

:class:`TestToleratedFailuresProveTheyFired` covers what structure cannot: a
handful of tests inject a failure and then assert *success*, because tolerating
that failure is the behaviour under test. Disarm the patch and they still pass,
having exercised nothing. Those tests must record that the stub ran and assert
it — and this guard finds the shape mechanically, so a new test of the same
shape cannot be added without the same assertion.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_ROOT = REPO_ROOT / "src"
TESTS_ROOT = REPO_ROOT / "tests"
FIRST_PARTY = "ferumind"

#: Call targets that replace an attribute on an object for the duration of a test.
PATCHING_CALLS = frozenset({"setattr", "delattr", "patch", "object"})

#: Names a test uses to record that an injected stub ran.
CALL_RECORD_HINTS = ("call", "ran", "fired")


@dataclass(frozen=True)
class PatchSite:
    """One place a test replaces an attribute on a first-party module."""

    module: str
    attribute: str
    origin: str

    def __str__(self) -> str:
        return f"{self.origin}: patches {self.module}.{self.attribute}"


def _module_path(dotted: str) -> Path | None:
    """Return the source file for *dotted*, or ``None`` if it is not a module."""
    relative = Path(*dotted.split("."))
    for candidate in (SRC_ROOT / relative.with_suffix(".py"), SRC_ROOT / relative / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """Map names bound in *tree* to the first-party modules they refer to."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if not node.module or node.module.split(".")[0] != FIRST_PARTY:
                continue
            for alias in node.names:
                dotted = f"{node.module}.{alias.name}"
                if _module_path(dotted) is not None:
                    aliases[alias.asname or alias.name] = dotted
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] != FIRST_PARTY:
                    continue
                if alias.asname and _module_path(alias.name) is not None:
                    aliases[alias.asname] = alias.name
    return aliases


def _string_target(dotted: str) -> tuple[str, str] | None:
    """Split ``"pkg.mod.attr"`` into its module and the attribute chain's root."""
    parts = dotted.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:cut])
        if _module_path(candidate) is not None:
            return candidate, parts[cut]
    return None


def _constant_str(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _patch_sites(scope: ast.AST, aliases: dict[str, str], origin: str) -> list[PatchSite]:
    """Return every first-party module attribute patched within *scope*."""
    sites: list[PatchSite] = []
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in PATCHING_CALLS:
            continue

        literal = _constant_str(node.args[0])
        if literal is not None:
            resolved = _string_target(literal)
            if resolved is not None:
                sites.append(PatchSite(resolved[0], resolved[1], f"{origin}:{node.lineno}"))
            continue

        if len(node.args) < 2 or not isinstance(node.args[0], ast.Name):
            continue
        module = aliases.get(node.args[0].id)
        attribute = _constant_str(node.args[1])
        if module is not None and attribute is not None:
            sites.append(PatchSite(module, attribute, f"{origin}:{node.lineno}"))
    return sites


def _all_patch_sites() -> list[PatchSite]:
    sites: list[PatchSite] = []
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        tree = _parse(path)
        origin = str(path.relative_to(REPO_ROOT))
        sites.extend(_patch_sites(tree, _module_aliases(tree), origin))
    return sites


def _reachable_attributes() -> dict[str, set[str]]:
    """Map each first-party module to the attribute names that reach its code.

    An attribute reaches a module's code two ways: the module reads it as a
    global (``save_registry(...)`` after a ``from`` import), or another module
    reads it off the module object (``upload_staging.remove_staging_dir(...)``).
    Patching is only effective in those two cases.
    """
    reachable: dict[str, set[str]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = _parse(path)
        dotted = ".".join(path.relative_to(SRC_ROOT).with_suffix("").parts)
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        own = reachable.setdefault(dotted, set())
        aliases = _module_aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                own.add(node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                target = aliases.get(node.value.id)
                if target is not None:
                    reachable.setdefault(target, set()).add(node.attr)
    return reachable


class TestPatchedSymbolsAreLive:
    """A patched attribute nothing reaches through that module is a dead patch."""

    def test_the_scanner_finds_the_known_patch_surface(self) -> None:
        """Guard the guard: a scanner that matched nothing would pass forever."""
        sites = _all_patch_sites()
        assert len(sites) >= 25, f"expected the known patch surface, found {len(sites)}"
        by_module: dict[str, set[str]] = {}
        for site in sites:
            by_module.setdefault(site.module, set()).add(site.attribute)

        # Seams followed their code out of `writes`, one extraction at a time.
        # Naming them per destination is what makes this assertion notice a
        # change that moves code but leaves the patch on the old module.
        assert {"record_operation"} <= by_module.get("ferumind.core.patch_writes", set())
        assert {"_episode_now", "record_snapshot_in_db"} <= by_module.get(
            "ferumind.core.document_writes", set()
        )
        assert {"fetch_remote_file", "record_snapshot_in_db", "MAX_CHUNK_BYTES"} <= by_module.get(
            "ferumind.core.upload_writes", set()
        )
        assert {"record_operation", "remove_from_index"} <= by_module.get(
            "ferumind.core.lifecycle_writes", set()
        )
        assert {"save_registry", "record_operation"} <= by_module.get(
            "ferumind.core.project_writes", set()
        )
        # `ferumind.core.writes` is gone: REL-027 took the last two domains out
        # and deleted the module rather than leaving a re-export shell. Nothing
        # may be patched on it again — a patch naming it now fails as "no such
        # module" in the sibling test below.
        assert "ferumind.core.writes" not in by_module

    def test_every_patched_attribute_reaches_its_module(self) -> None:
        reachable = _reachable_attributes()
        dead: list[str] = []
        for site in _all_patch_sites():
            if _module_path(site.module) is None:
                dead.append(f"{site} — no such module")
            elif site.attribute not in reachable.get(site.module, set()):
                dead.append(str(site))

        if dead:
            pytest.fail(
                "These tests patch an attribute that no longer reaches the module\n"
                "they patch it on, so the patch has no effect and the test proves\n"
                "nothing.\n\n"
                "This is the expected failure when an extraction (REL-024 to REL-027)\n"
                "moves code out of a module but leaves the symbol behind as a\n"
                "compatibility re-export. Re-point the patch at the module that now\n"
                "runs the code — never delete this assertion to make it pass.\n\n"
                + "\n".join(f"  - {line}" for line in sorted(dead))
            )


def _tolerated_failure_tests() -> list[tuple[str, str]]:
    """Return tests that inject a failure and then assert success.

    The shape: the test patches a first-party module, the replacement raises,
    and the test expects no error. Tolerating the failure *is* the behaviour
    under test, which is precisely why a disarmed patch leaves it green.
    """
    found: list[tuple[str, str]] = []
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        tree = _parse(path)
        aliases = _module_aliases(tree)
        origin = str(path.relative_to(REPO_ROOT))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_") or not _patch_sites(node, aliases, origin):
                continue
            source = ast.unparse(node)
            if "pytest.raises" in source or "pytest.fail" in source:
                continue
            stubs = [
                inner
                for inner in ast.walk(node)
                if isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef) and inner is not node
            ]
            if any(isinstance(x, ast.Raise) for stub in stubs for x in ast.walk(stub)):
                found.append((f"{origin}:{node.lineno}", node.name))
    return found


class TestToleratedFailuresProveTheyFired:
    """Tests that assert success under an injected failure must prove it fired."""

    def test_the_shape_still_exists_in_the_suite(self) -> None:
        assert _tolerated_failure_tests(), (
            "no tolerated-failure tests found; if the shape is genuinely gone, "
            "retire this guard deliberately rather than leaving it vacuous"
        )

    def test_each_one_asserts_its_injected_stub_ran(self) -> None:
        unproven: list[str] = []
        for path_and_line, name in _tolerated_failure_tests():
            module_path = REPO_ROOT / path_and_line.split(":")[0]
            tree = _parse(module_path)
            function = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
            )
            asserted = {
                child.id
                for statement in ast.walk(function)
                if isinstance(statement, ast.Assert)
                for child in ast.walk(statement.test)
                if isinstance(child, ast.Name)
            }
            if not any(hint in word for word in asserted for hint in CALL_RECORD_HINTS):
                unproven.append(f"{path_and_line} {name}")

        if unproven:
            pytest.fail(
                "These tests inject a failure and then assert success, so they pass\n"
                "unchanged if the monkeypatch stops biting — which is exactly what\n"
                "an extraction that re-points the symbol will cause.\n\n"
                "Each must record that its stub ran and assert the record, e.g.\n"
                "    calls: list[str] = []\n"
                "    def failing_stub(...): calls.append('x'); raise OSError(...)\n"
                "    ...\n"
                "    assert calls, 'the injected failure never fired'\n\n"
                + "\n".join(f"  - {line}" for line in sorted(unproven))
            )
