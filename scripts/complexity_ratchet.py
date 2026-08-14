#!/usr/bin/env python3
"""Hold the line on function complexity without failing pre-existing debt.

Ferumind carries a large amount of recorded complexity debt, most of it in
``core/writes.py``. Turning the complexity rules on repository-wide would fail
a tree nobody has broken, so the rules stay out of ``[tool.ruff.lint] select``
and this script enforces them as a **ratchet** instead: the violations recorded
in ``complexity-baseline.json`` are tolerated, and anything beyond them fails.

Three design choices carry the whole thing.

**The budgets live here, not in ``pyproject.toml``.** ``RULE_BUDGETS`` is passed
to Ruff with ``--config`` overrides on every run, so the numbers this script
enforces are the numbers it recorded, and no edit to ``pyproject.toml`` can
loosen them behind its back. ``--ignore-noqa`` closes the other bypass: a
``# noqa: C901`` on a new function would otherwise hide it from the ratchet.

**A baseline entry is keyed by rule and qualified function name — never by
file.** A per-file ignore list is the obvious approach and it is wrong here.
``core/writes.py`` is about to be split across four tickets (REL-024 to
REL-027); a file-keyed baseline would forgive every *new* function added to a
listed file, and would flag every behaviour-preserving *move* as a fresh
violation, which would block the extraction it is supposed to protect. Keying on
the function means a move costs nothing and a new over-budget function is caught
wherever it is written.

Dropping the module from the key does give up something, so the key carries an
occurrence ``count``. Moving ``create_project`` from one module to another keeps
the count at one and passes; *copying* it into a second module makes the count
two and fails. Debt can relocate, but it cannot multiply. The ``modules`` field
records where each entry currently lives: it is advisory for pass/fail, and a
relocation is reported rather than passed over in silence.

**Fixing debt fails the check until the improvement is recorded.** When a
baseline entry stops violating, the check fails with a stale-baseline error and
``--update`` re-records it. That is what makes this a ratchet rather than a
ceiling: without it, a resolved violation would leave a permanent licence to
reintroduce itself. ``--update`` only ever tightens — it refuses to run while
any regression is outstanding, so it can never be used to launder a new
violation into the baseline.

The upshot for REL-024 to REL-027, whose shared acceptance criterion is
"complexity decreases without creating a new god module": the shrinking diff of
``complexity-baseline.json`` is the proof, and the ``modules`` column shows
where the debt went.

Usage::

    uv run python scripts/complexity_ratchet.py            # check (verify.sh)
    uv run python scripts/complexity_ratchet.py --update    # re-record, tighter only
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess  # runs Ruff from this interpreter's environment; fixed argv
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "complexity-baseline.json"
SCAN_ROOT = "src"

#: Ruff rule -> (config key that sets its budget, budget). These are Ruff's own
#: defaults, chosen rather than inherited: passing them explicitly means a Ruff
#: release that changes a default cannot move the line this script holds.
RULE_BUDGETS: Mapping[str, tuple[str, int]] = {
    "C901": ("lint.mccabe.max-complexity", 10),
    "PLR0911": ("lint.pylint.max-returns", 6),
    "PLR0912": ("lint.pylint.max-branches", 12),
    "PLR0913": ("lint.pylint.max-args", 5),
    "PLR0915": ("lint.pylint.max-statements", 50),
}


class RatchetError(RuntimeError):
    """The complexity report could not be produced or read."""


@dataclass(frozen=True, order=True)
class BaselineEntry:
    """One unit of recorded debt: a rule broken by a named function."""

    rule: str
    function: str
    count: int
    modules: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str]:
        """Return the identity this entry is matched on (never includes a file)."""
        return (self.rule, self.function)

    def as_dict(self) -> dict[str, object]:
        """Return the JSON form, field order fixed for readable diffs."""
        return {
            "rule": self.rule,
            "function": self.function,
            "count": self.count,
            "modules": list(self.modules),
        }


@dataclass(frozen=True)
class Baseline:
    """The recorded debt, plus the budgets it was recorded against."""

    budgets: Mapping[str, int]
    entries: tuple[BaselineEntry, ...]

    @property
    def total(self) -> int:
        """Return the number of individual violations recorded."""
        return sum(entry.count for entry in self.entries)

    def by_key(self) -> Mapping[tuple[str, str], BaselineEntry]:
        """Return the entries indexed by ``(rule, function)``."""
        return {entry.key: entry for entry in self.entries}

    def as_dict(self) -> dict[str, object]:
        """Return the JSON document written to disk."""
        return {
            "_comment": (
                "Recorded complexity debt. Managed by scripts/complexity_ratchet.py; "
                "regenerate with --update, which only ever tightens. Entries are keyed "
                "by (rule, function) so a behaviour-preserving move between modules is "
                "free, while a copy raises the count and fails."
            ),
            "scan_root": SCAN_ROOT,
            "budgets": dict(sorted(self.budgets.items())),
            "total": self.total,
            "entries": [entry.as_dict() for entry in sorted(self.entries)],
        }


@dataclass(frozen=True)
class Comparison:
    """What changed between the recorded baseline and the current tree."""

    regressions: tuple[str, ...]
    resolved: tuple[str, ...]
    relocated: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        """Return whether the tree matches the baseline exactly."""
        return not self.regressions and not self.resolved


def qualified_function_lines(source: str, origin: str) -> Mapping[int, str]:
    """Map each ``def`` line in *source* to its qualified function name.

    Two functions cannot share a line in valid Python, so the line number is a
    unique key within a file, and Ruff reports these rules at the ``def`` line.
    Classes contribute their name to the path; nested functions are qualified by
    their enclosing function so an inner helper is never confused with a
    module-level one of the same name.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise RatchetError(f"{origin} is not parseable Python: {exc}") from exc

    names: dict[int, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                qualified = f"{prefix}{child.name}"
                names[child.lineno] = qualified
                walk(child, f"{qualified}.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return names


def _diagnostic_fields(raw: object, index: int) -> tuple[str, str, int]:
    """Return ``(code, filename, row)`` from one Ruff JSON diagnostic."""
    if not isinstance(raw, dict):
        raise RatchetError(f"Ruff diagnostic {index} is not an object")
    diagnostic = cast("dict[str, object]", raw)
    code = diagnostic.get("code")
    filename = diagnostic.get("filename")
    location = diagnostic.get("location")
    if not isinstance(code, str) or not isinstance(filename, str):
        raise RatchetError(f"Ruff diagnostic {index} has no code or filename")
    if not isinstance(location, dict):
        raise RatchetError(f"Ruff diagnostic {index} has no location")
    row = cast("dict[str, object]", location).get("row")
    if not isinstance(row, int):
        raise RatchetError(f"Ruff diagnostic {index} has no integer location row")
    return code, filename, row


def entries_from_diagnostics(
    diagnostics: Iterable[object], repo_root: Path
) -> tuple[BaselineEntry, ...]:
    """Resolve Ruff diagnostics to baseline entries.

    A diagnostic that cannot be tied to a function is fatal rather than dropped:
    silently skipping one would punch a hole in the ratchet exactly where the
    resolver is weakest.
    """
    sources: dict[Path, Mapping[int, str]] = {}
    counts: dict[tuple[str, str], int] = {}
    modules: dict[tuple[str, str], set[str]] = {}

    for index, raw in enumerate(diagnostics):
        code, filename, row = _diagnostic_fields(raw, index)
        path = Path(filename)
        if path not in sources:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RatchetError(f"could not read {filename}: {exc}") from exc
            sources[path] = qualified_function_lines(text, filename)
        function = sources[path].get(row)
        if function is None:
            raise RatchetError(
                f"{filename}:{row}: {code} could not be attributed to a function "
                "definition, so it cannot be recorded or enforced"
            )
        try:
            relative = PurePosixPath(path.resolve().relative_to(repo_root).as_posix())
        except ValueError as exc:
            raise RatchetError(f"{filename} is outside {repo_root}") from exc
        key = (code, function)
        counts[key] = counts.get(key, 0) + 1
        modules.setdefault(key, set()).add(str(relative))

    return tuple(
        sorted(
            BaselineEntry(
                rule=rule,
                function=function,
                count=count,
                modules=tuple(sorted(modules[(rule, function)])),
            )
            for (rule, function), count in counts.items()
        )
    )


def ruff_command(scan_root: str) -> tuple[str, ...]:
    """Return the Ruff invocation, budgets and noqa policy pinned explicitly."""
    command = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--select",
        ",".join(sorted(RULE_BUDGETS)),
        "--ignore-noqa",
        "--output-format",
        "json",
    ]
    for setting, budget in sorted(RULE_BUDGETS.values()):
        command.extend(["--config", f"{setting}={budget}"])
    command.append(scan_root)
    return tuple(command)


def run_ruff(repo_root: Path, scan_root: str = SCAN_ROOT) -> tuple[object, ...]:
    """Return Ruff's JSON diagnostics for the complexity rules."""
    command = ruff_command(scan_root)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RatchetError(f"could not run Ruff: {exc}") from exc
    # Ruff exits 1 when it finds violations, which is the normal case here.
    if result.returncode not in (0, 1):
        raise RatchetError(f"Ruff exited {result.returncode}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise RatchetError(f"Ruff did not return JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise RatchetError("Ruff did not return a JSON array of diagnostics")
    return tuple(cast("list[object]", payload))


def current_baseline(repo_root: Path, scan_root: str = SCAN_ROOT) -> Baseline:
    """Measure the tree at *repo_root* and return it as a baseline."""
    entries = entries_from_diagnostics(run_ruff(repo_root, scan_root), repo_root)
    return Baseline(
        budgets={rule: budget for rule, (_, budget) in RULE_BUDGETS.items()},
        entries=entries,
    )


def parse_baseline(text: str) -> Baseline:
    """Parse a baseline document, rejecting anything it cannot fully validate."""
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise RatchetError(f"the baseline is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RatchetError("the baseline is not a JSON object")
    document = cast("dict[str, object]", payload)

    raw_budgets = document.get("budgets")
    if not isinstance(raw_budgets, dict):
        raise RatchetError("the baseline records no budgets")
    budgets: dict[str, int] = {}
    for rule, budget in cast("dict[str, object]", raw_budgets).items():
        if not isinstance(budget, int):
            raise RatchetError(f"the baseline budget for {rule} is not an integer")
        budgets[rule] = budget

    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise RatchetError("the baseline has no entries list")
    entries: list[BaselineEntry] = []
    for index, raw in enumerate(cast("list[object]", raw_entries)):
        if not isinstance(raw, dict):
            raise RatchetError(f"baseline entry {index} is not an object")
        entry = cast("dict[str, object]", raw)
        rule = entry.get("rule")
        function = entry.get("function")
        count = entry.get("count")
        modules = entry.get("modules")
        if not isinstance(rule, str) or not isinstance(function, str):
            raise RatchetError(f"baseline entry {index} has no rule or function")
        if not isinstance(count, int) or count < 1:
            raise RatchetError(f"baseline entry {index} has no positive count")
        if not isinstance(modules, list):
            raise RatchetError(f"baseline entry {index} has no modules list")
        entries.append(
            BaselineEntry(
                rule=rule,
                function=function,
                count=count,
                modules=tuple(
                    item for item in cast("list[object]", modules) if isinstance(item, str)
                ),
            )
        )
    return Baseline(budgets=budgets, entries=tuple(sorted(entries)))


def load_baseline(path: Path) -> Baseline:
    """Read the recorded baseline from *path*."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RatchetError(
            f"could not read {path}: {exc}. Record one with "
            "'uv run python scripts/complexity_ratchet.py --update'."
        ) from exc
    return parse_baseline(text)


def write_baseline(path: Path, baseline: Baseline) -> None:
    """Write *baseline* to *path* as formatted, newline-terminated JSON."""
    path.write_text(json.dumps(baseline.as_dict(), indent=2) + "\n", encoding="utf-8")


def compare(baseline: Baseline, current: Baseline) -> Comparison:
    """Return how *current* differs from *baseline*."""
    recorded = baseline.by_key()
    measured = current.by_key()

    regressions: list[str] = []
    for rule, budget in sorted(current.budgets.items()):
        if baseline.budgets.get(rule) != budget:
            regressions.append(
                f"budget drift: {rule} is enforced at {budget} but the baseline "
                f"records {baseline.budgets.get(rule)}"
            )
    for key, entry in sorted(measured.items()):
        previous = recorded.get(key)
        where = ", ".join(entry.modules)
        if previous is None:
            regressions.append(
                f"{entry.rule} {entry.function} ({where}): new violation, not in the baseline"
            )
        elif entry.count > previous.count:
            regressions.append(
                f"{entry.rule} {entry.function} ({where}): {entry.count} occurrences, "
                f"baseline records {previous.count} — debt was copied, not moved"
            )

    resolved: list[str] = []
    for key, entry in sorted(recorded.items()):
        now = measured.get(key)
        if now is None:
            resolved.append(f"{entry.rule} {entry.function}: no longer violates")
        elif now.count < entry.count:
            resolved.append(
                f"{entry.rule} {entry.function}: {now.count} occurrences, "
                f"baseline records {entry.count}"
            )

    relocated: list[str] = []
    for key, entry in sorted(measured.items()):
        previous = recorded.get(key)
        if previous is not None and previous.modules != entry.modules:
            relocated.append(
                f"{entry.rule} {entry.function}: {', '.join(previous.modules)} -> "
                f"{', '.join(entry.modules)}"
            )

    return Comparison(
        regressions=tuple(regressions),
        resolved=tuple(resolved),
        relocated=tuple(relocated),
    )


def _report(comparison: Comparison, baseline: Baseline, current: Baseline) -> int:
    """Print the comparison and return the process exit code."""
    # Relocations are context for whichever verdict follows, so they share its
    # stream — otherwise stdout and stderr interleave and read out of order.
    stream = sys.stdout if comparison.is_clean else sys.stderr
    for line in comparison.relocated:
        print(f"  moved: {line}", file=stream)

    if comparison.regressions:
        print("Complexity ratchet failed — new debt:", file=sys.stderr)
        for line in comparison.regressions:
            print(f"  - {line}", file=sys.stderr)
        print(
            "\nNew or materially changed functions must meet the budget: "
            + ", ".join(f"{rule} <= {budget}" for rule, budget in sorted(current.budgets.items()))
            + ".\nSplit the function or reduce its arguments. Re-recording the "
            "baseline is not a way out: --update refuses while a regression stands.",
            file=sys.stderr,
        )
        return 1

    if comparison.resolved:
        print("Complexity ratchet: the baseline is stale — debt was fixed:", file=sys.stderr)
        for line in comparison.resolved:
            print(f"  - {line}", file=sys.stderr)
        print(
            f"\nRecorded total {baseline.total}, measured {current.total}. Lock the "
            "improvement in with 'uv run python scripts/complexity_ratchet.py --update' "
            "and commit the baseline, so the violation cannot come back unnoticed.",
            file=sys.stderr,
        )
        return 1

    print(f"Complexity ratchet passed: {current.total} recorded violations, none added.")
    return 0


def check(repo_root: Path, baseline_path: Path) -> int:
    """Compare the tree against the recorded baseline."""
    baseline = load_baseline(baseline_path)
    current = current_baseline(repo_root)
    return _report(compare(baseline, current), baseline, current)


def update(repo_root: Path, baseline_path: Path) -> int:
    """Re-record the baseline, refusing anything that is not a tightening."""
    current = current_baseline(repo_root)
    if baseline_path.is_file():
        baseline = load_baseline(baseline_path)
        comparison = compare(baseline, current)
        if comparison.regressions:
            print(
                "Refusing to update the baseline: the tree has new debt, and "
                "recording it would defeat the ratchet.",
                file=sys.stderr,
            )
            for line in comparison.regressions:
                print(f"  - {line}", file=sys.stderr)
            return 1
        if current.total > baseline.total:
            print(
                f"Refusing to update the baseline: total would rise from "
                f"{baseline.total} to {current.total}.",
                file=sys.stderr,
            )
            return 1
        print(f"Complexity baseline: {baseline.total} -> {current.total} violations.")
    else:
        print(f"Complexity baseline recorded: {current.total} violations.")
    write_baseline(baseline_path, current)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--update",
        action="store_true",
        help="Re-record the baseline. Only ever tightens; refuses on regressions.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository to measure (default: this checkout)",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=BASELINE_PATH,
        help=f"Baseline document (default: {BASELINE_PATH.name})",
    )
    args = parser.parse_args(argv)

    repo_root: Path = args.repo_root
    baseline_path: Path = args.baseline
    try:
        if args.update:
            return update(repo_root.resolve(), baseline_path)
        return check(repo_root.resolve(), baseline_path)
    except RatchetError as exc:
        print(f"Complexity ratchet error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
