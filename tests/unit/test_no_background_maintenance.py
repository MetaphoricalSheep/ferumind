"""Guards: no background maintenance; section_index stays indexer-owned."""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE = REPO_ROOT / "src" / "ferumind" / "core"

#: Modules that maintain derived index state. Background scheduling here would
#: reintroduce the watcher/daemon layer the rebuild removed.
_INDEX_MAINTENANCE_MODULES = (
    CORE / "indexer.py",
    CORE / "reconcile.py",
    CORE / "write_common.py",
)

_FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "threading",
        "multiprocessing",
        "sched",
        "asyncio",
        "subprocess",
        "watchdog",
        "apscheduler",
        "schedule",
    }
)

_FORBIDDEN_NAME = re.compile(r"(?i)(watcher|daemon|scheduler|poll_loop)")

_SECTION_INDEX_ALLOWED = (
    "src/ferumind/core/indexer.py",
    "src/ferumind/core/search.py",
    "src/ferumind/core/verify_index.py",
    # Project deletion must clear derived rows for the project it removes.
    # Omitting it here is what let ``delete_project`` leave orphan section rows
    # behind, visible only to ``verify-index``.
    "src/ferumind/core/project_admin.py",
    "src/ferumind/db/schema.sql",
    "src/ferumind/db/migrations/0002_section_index.sql",
    "scripts/check_distribution.py",
)


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _defined_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def background_maintenance_violations(source: str) -> tuple[str, ...]:
    """Return human-readable violations for *source* (one module)."""
    tree = ast.parse(source)
    violations: list[str] = []
    for root in sorted(_import_roots(tree) & _FORBIDDEN_IMPORT_ROOTS):
        violations.append(f"forbidden import root {root!r}")
    for name in sorted(_defined_names(tree)):
        if _FORBIDDEN_NAME.search(name):
            violations.append(f"forbidden defined name {name!r}")
    return tuple(violations)


def test_index_maintenance_modules_have_no_background_scheduling() -> None:
    for path in _INDEX_MAINTENANCE_MODULES:
        assert path.is_file(), path
        violations = background_maintenance_violations(path.read_text(encoding="utf-8"))
        assert violations == (), f"{path.relative_to(REPO_ROOT)}: {violations}"


def test_background_maintenance_guard_fires_on_counterexample() -> None:
    counterexample = (
        "import threading\n"
        "from watchdog.observers import Observer\n"
        "\n"
        "def start_watcher() -> None:\n"
        "    pass\n"
        "\n"
        "daemon = True\n"
    )
    violations = background_maintenance_violations(counterexample)
    assert "forbidden import root 'threading'" in violations
    assert "forbidden import root 'watchdog'" in violations
    assert any("start_watcher" in item for item in violations)
    assert any("daemon" in item for item in violations)


def test_pyproject_does_not_depend_on_background_schedulers() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for package in ("watchdog", "apscheduler", "schedule"):
        assert package not in text.lower()


_SECTION_INDEX_TABLE_MENTION = re.compile(
    r"(?:FROM|INTO|TABLE|JOIN|UPDATE|DELETE\s+FROM)\s+section_index\b"
    r"|['\"]section_index['\"]"
    r"|section_index\.sql"
    r"|/section_index\b"
)


def test_section_index_is_only_referenced_from_allowed_paths() -> None:
    """Section state: indexer writes; search and verify-index read; delete clears."""
    allowed = {REPO_ROOT / relative for relative in _SECTION_INDEX_ALLOWED}
    allowed.add(Path(__file__))
    offenders: list[str] = []
    scan_roots = (
        REPO_ROOT / "src",
        REPO_ROOT / "tests",
        REPO_ROOT / "scripts",
    )
    for root in scan_roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".sql", ".md"}:
                continue
            if path in allowed:
                continue
            if path.name in {
                "test_section_index.py",
                "test_database.py",
                "test_search.py",
                "test_mcp_surface.py",
                "test_verify_index.py",
                "test_project_admin.py",
            }:
                continue
            text = path.read_text(encoding="utf-8")
            if _SECTION_INDEX_TABLE_MENTION.search(text):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []
