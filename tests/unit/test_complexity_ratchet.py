"""The complexity ratchet: what it forgives, what it catches, and what it refuses.

The ratchet exists to let ``core/writes.py`` be split across REL-024 to REL-027
without either blocking the extraction or going blind during it. Three
behaviours carry that, and each has a test here:

* a function that **moves** between modules keeps its identity and passes,
* a function that is **copied** raises its occurrence count and fails,
* a violation that is **fixed** fails as a stale baseline until re-recorded, so
  the improvement is locked in rather than left as a standing licence.

The last test runs the real check against the real tree, which is what puts the
ratchet in the pre-commit hook as well as ``scripts/verify.sh``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from complexity_ratchet import (
    BASELINE_PATH,
    Baseline,
    BaselineEntry,
    RatchetError,
    check,
    compare,
    current_baseline,
    entries_from_diagnostics,
    main,
    parse_baseline,
    qualified_function_lines,
    ruff_command,
    update,
    write_baseline,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Eight parameters against a budget of five, and a branch thicket over the
# complexity budget of ten: one function, two rules, no imports.
OVER_BUDGET = '''
def sprawls(a, b, c, d, e, f, g, h):
    """Over the argument and complexity budgets at once."""
    total = 0
    for value in (a, b, c, d, e, f, g, h):
        if value == 1:
            total += 1
        elif value == 2:
            total += 2
        elif value == 3:
            total += 3
        elif value == 4:
            total += 4
        elif value == 5:
            total += 5
        elif value == 6:
            total += 6
        elif value == 7:
            total += 7
        elif value == 8:
            total += 8
        elif value == 9:
            total += 9
        elif value == 10:
            total += 10
    return total
'''

WITHIN_BUDGET = '''
def modest(a, b):
    """Comfortably inside every budget."""
    return a + b
'''


def _tree(root: Path, **modules: str) -> Path:
    """Write *modules* under ``root/src`` and return *root*."""
    package = root / "src" / "sample"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, source in modules.items():
        (package / f"{name}.py").write_text(source, encoding="utf-8")
    return root


def _entry(rule: str, function: str, count: int = 1, *modules: str) -> BaselineEntry:
    return BaselineEntry(rule=rule, function=function, count=count, modules=modules or ("m.py",))


def _baseline(*entries: BaselineEntry) -> Baseline:
    return Baseline(
        budgets={"C901": 10, "PLR0911": 6, "PLR0912": 12, "PLR0913": 5, "PLR0915": 50},
        entries=entries,
    )


class TestQualifiedNames:
    """Diagnostics are attributed to a function, never to a line in a file."""

    def test_methods_and_nested_functions_are_qualified(self) -> None:
        source = "class Outer:\n    def method(self):\n        def inner():\n            pass\n"
        names = qualified_function_lines(source, "sample.py")
        assert names == {2: "Outer.method", 3: "Outer.method.inner"}

    def test_unparseable_source_is_an_error_not_a_silent_skip(self) -> None:
        with pytest.raises(RatchetError, match="not parseable"):
            qualified_function_lines("def (:", "broken.py")

    def test_a_diagnostic_that_hits_no_function_is_fatal(self, tmp_path: Path) -> None:
        """A dropped diagnostic would be a hole in the ratchet, so it raises."""
        module = tmp_path / "m.py"
        module.write_text("x = 1\n", encoding="utf-8")
        diagnostic = {"code": "C901", "filename": str(module), "location": {"row": 1}}
        with pytest.raises(RatchetError, match="could not be attributed to a function"):
            entries_from_diagnostics([diagnostic], tmp_path)

    @pytest.mark.parametrize(
        "diagnostic",
        [
            "not-an-object",
            {"filename": "m.py", "location": {"row": 1}},
            {"code": "C901", "filename": "m.py"},
            {"code": "C901", "filename": "m.py", "location": {"row": "one"}},
        ],
    )
    def test_malformed_diagnostics_are_rejected(self, diagnostic: object, tmp_path: Path) -> None:
        with pytest.raises(RatchetError):
            entries_from_diagnostics([diagnostic], tmp_path)


class TestBudgetsArePinnedByTheScript:
    """The enforced budget cannot be loosened from ``pyproject.toml``."""

    def test_every_rule_is_passed_with_an_explicit_budget(self) -> None:
        command = ruff_command("src")
        assert "--ignore-noqa" in command, "a noqa comment must not hide a new violation"
        for setting, budget in (
            ("lint.mccabe.max-complexity", 10),
            ("lint.pylint.max-returns", 6),
            ("lint.pylint.max-branches", 12),
            ("lint.pylint.max-args", 5),
            ("lint.pylint.max-statements", 50),
        ):
            assert f"{setting}={budget}" in command

    def test_a_baseline_recorded_against_other_budgets_fails(self) -> None:
        """Changing the budget must go through a reviewed re-record."""
        loosened = Baseline(budgets={"PLR0913": 12}, entries=())
        comparison = compare(loosened, _baseline())
        assert any("budget drift" in line for line in comparison.regressions)


class TestTheRatchetHolds:
    """New debt fails; the shape of the failure names the function."""

    def test_a_new_over_budget_function_fails_and_is_named(self, tmp_path: Path) -> None:
        _tree(tmp_path, fresh=OVER_BUDGET)
        comparison = compare(_baseline(), current_baseline(tmp_path))
        assert any(
            "sprawls" in line and "new violation" in line for line in comparison.regressions
        ), comparison.regressions

    def test_an_unchanged_recorded_violation_passes(self, tmp_path: Path) -> None:
        _tree(tmp_path, legacy=OVER_BUDGET)
        measured = current_baseline(tmp_path)
        assert compare(measured, measured).is_clean

    def test_code_within_budget_records_nothing(self, tmp_path: Path) -> None:
        _tree(tmp_path, clean=WITHIN_BUDGET)
        assert current_baseline(tmp_path).entries == ()


class TestExtractionIsNotBlocked:
    """The property that makes REL-024 to REL-027 possible at all."""

    def test_moving_a_function_to_a_new_module_passes(self, tmp_path: Path) -> None:
        """A per-file baseline would fail this. That is why the key is the function."""
        before = _tree(tmp_path / "before", god=OVER_BUDGET)
        after = _tree(tmp_path / "after", god=WITHIN_BUDGET, extracted=OVER_BUDGET)
        comparison = compare(current_baseline(before), current_baseline(after))
        assert comparison.is_clean, comparison.regressions + comparison.resolved
        assert any("god.py" in line and "extracted.py" in line for line in comparison.relocated), (
            "a move must be reported, not passed over in silence"
        )

    def test_copying_a_function_instead_of_moving_it_fails(self, tmp_path: Path) -> None:
        """Debt may relocate. It may not multiply."""
        before = _tree(tmp_path / "before", god=OVER_BUDGET)
        after = _tree(tmp_path / "after", god=OVER_BUDGET, extracted=OVER_BUDGET)
        comparison = compare(current_baseline(before), current_baseline(after))
        assert any(
            "sprawls" in line and "copied, not moved" in line for line in comparison.regressions
        ), comparison.regressions

    def test_a_new_over_budget_function_in_a_recorded_module_still_fails(
        self, tmp_path: Path
    ) -> None:
        """The loophole a per-file ignore list would leave open."""
        before = _tree(tmp_path / "before", god=OVER_BUDGET)
        after = _tree(tmp_path / "after", god=OVER_BUDGET + WITHIN_BUDGET.replace("modest", "x"))
        added = OVER_BUDGET.replace("sprawls", "added_later")
        (after / "src" / "sample" / "god.py").write_text(OVER_BUDGET + added, encoding="utf-8")
        comparison = compare(current_baseline(before), current_baseline(after))
        assert any(
            "added_later" in line and "new violation" in line for line in comparison.regressions
        ), comparison.regressions


class TestTheRatchetOnlyTightens:
    """A resolved violation must not leave a licence to reintroduce it."""

    def test_fixing_debt_reports_a_stale_baseline(self, tmp_path: Path) -> None:
        before = _tree(tmp_path / "before", god=OVER_BUDGET)
        after = _tree(tmp_path / "after", god=WITHIN_BUDGET)
        comparison = compare(current_baseline(before), current_baseline(after))
        assert not comparison.regressions
        assert any("sprawls" in line for line in comparison.resolved)
        assert not comparison.is_clean, "a stale baseline must not report success"

    def test_check_exits_nonzero_while_the_baseline_is_stale(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, god=WITHIN_BUDGET)
        baseline = root / "baseline.json"
        write_baseline(baseline, _baseline(_entry("C901", "sprawls")))
        assert check(root, baseline) == 1

    def test_update_refuses_to_record_a_regression(self, tmp_path: Path) -> None:
        """``--update`` must never be the way out of a failing ratchet."""
        root = _tree(tmp_path, god=OVER_BUDGET)
        baseline = root / "baseline.json"
        write_baseline(baseline, _baseline())
        assert update(root, baseline) == 1
        assert parse_baseline(baseline.read_text(encoding="utf-8")).entries == ()

    def test_update_refuses_to_raise_the_total(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, god=OVER_BUDGET)
        baseline = root / "baseline.json"
        # Same functions, but recorded as fewer occurrences than the tree has.
        write_baseline(baseline, _baseline(_entry("PLR0913", "sprawls")))
        assert update(root, baseline) == 1

    def test_update_records_the_improvement(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, god=WITHIN_BUDGET)
        baseline = root / "baseline.json"
        write_baseline(baseline, _baseline(_entry("C901", "sprawls")))
        assert update(root, baseline) == 0
        assert parse_baseline(baseline.read_text(encoding="utf-8")).total == 0
        assert check(root, baseline) == 0

    def test_a_first_baseline_can_be_recorded_from_nothing(self, tmp_path: Path) -> None:
        root = _tree(tmp_path, god=OVER_BUDGET)
        baseline = root / "baseline.json"
        assert update(root, baseline) == 0
        assert parse_baseline(baseline.read_text(encoding="utf-8")).total == 2


class TestBaselineDocument:
    """The recorded file is the reviewable artifact, so it must round-trip."""

    def test_round_trips_through_json(self, tmp_path: Path) -> None:
        original = _baseline(_entry("C901", "b", 2, "x.py", "y.py"), _entry("PLR0913", "a"))
        path = tmp_path / "baseline.json"
        write_baseline(path, original)
        assert parse_baseline(path.read_text(encoding="utf-8")) == original

    def test_entries_are_sorted_so_diffs_stay_reviewable(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        write_baseline(path, _baseline(_entry("PLR0913", "z"), _entry("C901", "a")))
        document = json.loads(path.read_text(encoding="utf-8"))
        assert [entry["function"] for entry in document["entries"]] == ["a", "z"]

    @pytest.mark.parametrize(
        "document",
        [
            "not json",
            "[]",
            '{"entries": []}',
            '{"budgets": {"C901": "ten"}, "entries": []}',
            '{"budgets": {}, "entries": "none"}',
            '{"budgets": {}, "entries": [{"rule": "C901"}]}',
            '{"budgets": {}, "entries": [{"rule": "C901", "function": "f", "count": 0}]}',
            '{"budgets": {}, "entries": [{"rule": "C901", "function": "f", "count": 1}]}',
        ],
    )
    def test_a_baseline_it_cannot_fully_validate_is_rejected(self, document: str) -> None:
        with pytest.raises(RatchetError):
            parse_baseline(document)

    def test_a_missing_baseline_says_how_to_record_one(self, tmp_path: Path) -> None:
        with pytest.raises(RatchetError, match="--update"):
            check(tmp_path, tmp_path / "absent.json")

    def test_main_reports_a_ratchet_error_without_a_traceback(self, tmp_path: Path) -> None:
        assert (
            main(["--repo-root", str(tmp_path), "--baseline", str(tmp_path / "absent.json")]) == 1
        )


class TestTheCommittedBaseline:
    """The real tree against the real recorded baseline."""

    def test_the_repository_passes_its_own_ratchet(self) -> None:
        assert check(REPO_ROOT, BASELINE_PATH) == 0

    def test_the_recorded_total_matches_the_recorded_entries(self) -> None:
        document = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        baseline = parse_baseline(BASELINE_PATH.read_text(encoding="utf-8"))
        assert document["total"] == baseline.total

    def test_the_god_module_is_gone_and_stays_gone(self) -> None:
        """The inverse of the assertion REL-024 to REL-027 were measured against.

        Until REL-027 this asserted ``core/writes.py`` still *held* recorded
        debt, and said in its own message that reaching zero meant the
        extraction was done and the assertion should be retired with it. REL-027
        took the last two domains out and deleted the module rather than leaving
        a re-export shell, so the guard is turned around: the file must not come
        back, and no baseline entry may name it again.
        """
        baseline = parse_baseline(BASELINE_PATH.read_text(encoding="utf-8"))
        recorded = [
            f"{entry.rule} {entry.function}"
            for entry in baseline.entries
            if "src/ferumind/core/writes.py" in entry.modules
        ]
        assert not recorded, f"core/writes.py is gone; the baseline still names it: {recorded}"
        assert not (REPO_ROOT / "src/ferumind/core/writes.py").exists(), (
            "core/writes.py was deleted by REL-027; its five domains live in "
            "patch_writes, document_writes, upload_writes, lifecycle_writes, "
            "and project_writes"
        )
