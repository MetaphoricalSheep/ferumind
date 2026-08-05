"""Regression guard against unsafe string-prefix path containment checks.

Scans security-sensitive source files for ``.startswith()`` calls that could
be used for path containment — a pattern that produces false positives for
sibling-prefix paths (e.g. ``/tmp/root-evil`` vs ``/tmp/root``).

All legitimate uses must be allowlisted with a comment explaining why they
are safe.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SECURITY_SENSITIVE_MODULES: list[str] = [
    "src/ferumind/core/paths.py",
    "src/ferumind/core/security.py",
    "src/ferumind/core/projects.py",
    "src/ferumind/mcp/",
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Allowlisted call chains that are known to be safe.
# Each entry is a dict with:
#   - file: relative path from repo root
#   - line: the exact line text (leading whitespace stripped)
#   - reason: why this usage is safe
ALLOWLIST: list[dict[str, str]] = [
    {
        "file": "src/ferumind/mcp/read_tools.py",
        "line": "if rel_str.startswith(excluded):",
        "reason": (
            "Excluded dir prefix check on a project-relative path (produced by"
            " Path.relative_to, not user input). Safe."
        ),
    },
    {
        "file": "src/ferumind/mcp/read_tools.py",
        "line": 'if any(part.startswith(".") and part != ".ferumind" for part in parts):',
        "reason": (
            "Hidden file detection (dot-prefix check on path components),"
            " NOT a path containment check. Safe."
        ),
    },
    {
        "file": "src/ferumind/mcp/resources.py",
        "line": "if not uri_text.startswith(FILE_URI_PREFIX):",
        "reason": (
            "URI scheme dispatch on 'ferumind://file/', NOT a path containment"
            " check. Decides whether this handler owns the URI or delegates to"
            " FastMCP. The encoded path inside is decoded by parse_file_uri and"
            " then resolved through contained_path, which is where containment"
            " is actually enforced. Safe."
        ),
    },
]


def _collect_target_files() -> list[Path]:
    """Return the list of files to scan."""
    files: list[Path] = []
    for rel in SECURITY_SENSITIVE_MODULES:
        path = REPO_ROOT / rel
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        elif path.is_file():
            files.append(path)
    return files


def _iter_startswith_calls(
    file_path: Path,
) -> list[tuple[int, str]]:
    """Return (line_number, stripped_line) for lines containing ``.startswith(``."""
    text = file_path.read_text(encoding="utf-8")
    # Use the AST to be more precise: find Attribute calls for .startswith
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # If we can't parse, fall back to text search
        pass
    else:
        return _find_startswith_in_ast(tree, file_path)

    # Fallback: line-based grep
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        if ".startswith(" in line:
            hits.append((i, line.strip()))
    return hits


def _find_startswith_in_ast(tree: ast.AST, file_path: Path) -> list[tuple[int, str]]:
    """Use AST to find ``.startswith(`` calls."""
    hits: list[tuple[int, str]] = []

    class StartswithVisitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute) and node.func.attr == "startswith":
                line_text = _get_line(file_path, node.lineno)
                hits.append((node.lineno, line_text))
            self.generic_visit(node)

    StartswithVisitor().visit(tree)
    return hits


def _get_line(file_path: Path, lineno: int) -> str:
    with open(file_path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if i == lineno:
                return line.strip()
    return ""


def _is_allowlisted(rel_path: str, line: str) -> bool:
    stripped = line.strip()
    return any(entry["file"] == rel_path and entry["line"] == stripped for entry in ALLOWLIST)


def test_no_unsafe_path_prefix_checks() -> None:
    """No security-sensitive module should use ``.startswith()`` for path containment.

    String-prefix checks produce false positives with sibling paths
    (e.g. ``/tmp/repo-evil`` passes a ``.startswith("/tmp/repo")`` check).
    Use ``is_under_root()`` from ``ferumind.core.paths`` instead.
    """
    violations: list[str] = []

    for file_path in _collect_target_files():
        rel_path = str(file_path.relative_to(REPO_ROOT))
        for lineno, line_text in _iter_startswith_calls(file_path):
            if _is_allowlisted(rel_path, line_text):
                continue
            violations.append(f"  {rel_path}:{lineno}: {line_text}")

    if violations:
        msg = (
            "Found .startswith() calls in security-sensitive modules.\n"
            "String-prefix path containment checks are forbidden — they produce\n"
            "false positives with sibling-prefix paths (e.g. /tmp/repo-evil\n"
            "incorrectly passes a .startswith('/tmp/repo') check).\n\n"
            "Use is_under_root() from ferumind.core.paths instead.\n\n"
            "If the usage is legitimate and NOT a path containment check, add an\n"
            "entry to ALLOWLIST in this test file with an explanation.\n\n"
            "Violations:\n" + "\n".join(violations)
        )
        pytest.fail(msg)
