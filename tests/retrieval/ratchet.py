"""A recorded baseline, not an absolute threshold.

A hardcoded pass mark ("top-5 must exceed 80%") is either so loose it never
fires or needs editing on every improvement, and in both cases it stops meaning
anything. So this follows the contract ``scripts/complexity_ratchet.py`` already
runs in this repository:

* a regression against the recorded numbers **fails**;
* an *improvement* also fails, as a stale baseline, until it is re-recorded —
  otherwise a resolved gap leaves a permanent licence to reintroduce itself;
* ``--update`` only ever tightens, and **refuses while a regression stands**, so
  it can never be used to launder one into the baseline.

Two things are compared besides the numbers. The **corpus hash**, because
metrics from a different corpus are not comparable to these at all and a silent
pass would be worse than a failure. And the **SQLite version**, which is
advisory: it does not fail on its own, but it is named in any failure it could
explain, because the 3.12/3.13/3.14 matrix does not guarantee one FTS5 build.

Two deliberate escape hatches exist, each narrow:

* the one-way ``document`` → ``section`` granularity transition skips section
  metrics that were previously degenerate;
* ``--accept-corpus-change`` re-records after an intentional corpus replacement,
  stating that the old and new numbers are not comparable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from tests.retrieval.labels import CATEGORIES, PHRASINGS, Category, Phrasing
from tests.retrieval.scorer import Counts, Granularity, RunMetrics

REPO_ROOT: Final = Path(__file__).resolve().parent.parent.parent
BASELINE_PATH: Final = REPO_ROOT / "retrieval-baseline.json"
UPDATE_COMMAND: Final = "just retrieval-update"
CORPUS_UPDATE_COMMAND: Final = "just retrieval-update-corpus"

#: Metrics where a larger number is a better outcome.
HIGHER_IS_BETTER: Final[tuple[str, ...]] = (
    "document_top1",
    "document_top5",
    "document_top10",
    "section_top1",
    "section_top5",
    "section_top10",
    "evidence_in_snippet",
    "also_relevant_returned",
)

#: Section metrics become genuinely stricter the first time results carry a
#: section id. Diffing them against the old degenerate values would invent a
#: regression the harness itself was designed to produce.
SECTION_METRICS: Final[tuple[str, ...]] = (
    "section_top1",
    "section_top5",
    "section_top10",
)

#: Recorded and reported, but **never** pass/fail in either direction.
#:
#: Payload size is an observation, not an enforcement point. Real response
#: budgets belong at the ingress, and this must not grow into one. There is also a
#: mechanical reason: bytes rise with recall. A search that starts returning the
#: correct document *in addition* to what it already returned has improved and
#: got bigger at the same time, so treating growth as a regression would make
#: the ratchet punish exactly the change it exists to protect.
#:
#: Candidate-generation diagnostics are observed for the same reason: ratcheting
#: nonempty rates upward would reward returning junk merely to avoid a zero.
OBSERVED_ONLY: Final[tuple[str, ...]] = (
    "payload_bytes",
    "zero_result_queries",
    "nonempty_without_gold",
    "nonempty_with_gold",
    "results_returned",
)

CORPUS_NONCOMPARABLE_NOTE: Final = (
    "the corpus or its labels changed since the baseline was recorded, so "
    "these numbers describe a different corpus and cannot be compared — "
    "re-record with --update once the corpus is settled, or use "
    f"'{CORPUS_UPDATE_COMMAND}' for an intentional corpus replacement"
)

CORPUS_ACCEPT_MESSAGE: Final = (
    "Corpus changed: the previous baseline and the new numbers are not "
    "directly comparable. Recording a new baseline for the new corpus."
)


class BaselineMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The FTS5 build the numbers were measured on. Advisory: named in failures
    #: it could explain, never a failure by itself.
    sqlite_version: str
    #: Fixtures and labels together. A change here means the numbers describe a
    #: different corpus and cannot be compared.
    corpus_sha256: str
    granularity: Granularity
    query_count: int


class Baseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metadata: BaselineMetadata
    per_category: Mapping[Category, Counts]
    per_phrasing: Mapping[Phrasing, Counts]
    total: Counts


@dataclass(frozen=True, slots=True)
class Comparison:
    regressions: tuple[str, ...]
    improvements: tuple[str, ...]
    #: Recorded movements that are never pass/fail (see OBSERVED_ONLY).
    observations: tuple[str, ...]
    notes: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        return not self.regressions and not self.improvements

    @property
    def corpus_changed(self) -> bool:
        return any("describe a different corpus" in line for line in self.regressions)


def corpus_fingerprint(corpus_root: Path, queries_path: Path) -> str:
    """Hash the fixtures and their labels together.

    Path-and-content, in sorted path order, so the digest is stable across
    filesystems and changes whenever a document or a label does.
    """
    digest = hashlib.sha256()
    for file in sorted(corpus_root.rglob("*.md")):
        digest.update(file.relative_to(corpus_root).as_posix().encode("utf-8"))
        digest.update(file.read_bytes())
    digest.update(b"queries.yaml")
    digest.update(queries_path.read_bytes())
    return digest.hexdigest()


def baseline_from(metrics: RunMetrics, *, sqlite_version: str, corpus_sha256: str) -> Baseline:
    return Baseline(
        metadata=BaselineMetadata(
            sqlite_version=sqlite_version,
            corpus_sha256=corpus_sha256,
            granularity=metrics.granularity,
            query_count=metrics.total.queries,
        ),
        per_category=metrics.per_category,
        per_phrasing=metrics.per_phrasing,
        total=metrics.total,
    )


def load_baseline(path: Path) -> Baseline:
    if not path.is_file():
        msg = (
            f"No recorded baseline at {path}. Record one with "
            "'uv run python scripts/retrieval_report.py --update'."
        )
        raise FileNotFoundError(msg)
    return Baseline.model_validate_json(path.read_text(encoding="utf-8"))


def write_baseline(path: Path, baseline: Baseline) -> None:
    """Serialise deterministically: sorted keys, fixed indent, trailing newline."""
    payload = json.loads(baseline.model_dump_json())
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")


def update_baseline(
    path: Path,
    current: Baseline,
    *,
    accept_corpus_change: bool = False,
) -> Comparison:
    """Re-record the baseline, refusing anything that is not a tightening.

    This is the guard that makes the whole thing a ratchet rather than a
    ceiling. Without it, ``--update`` after a regression would silently write
    the worse numbers down as the new normal, and the regression would be gone
    from the record rather than fixed. Raises rather than returning a code so a
    caller cannot ignore it by accident.

    ``accept_corpus_change`` is the deliberate corpus-replacement path: when the
    corpus hash differs it records a new baseline and states non-comparability.
    On an *unchanged* corpus it still refuses ordinary retrieval regressions —
    accepting a corpus change accepts non-comparability, not worse retrieval on
    the same corpus.
    """
    if path.is_file():
        recorded = load_baseline(path)
        comparison = compare(recorded, current)
        if accept_corpus_change and comparison.corpus_changed:
            write_baseline(path, current)
            return Comparison(
                regressions=(),
                improvements=(),
                observations=comparison.observations,
                notes=(*comparison.notes, CORPUS_ACCEPT_MESSAGE),
            )
        if comparison.regressions:
            detail = "\n  - ".join(comparison.regressions)
            msg = (
                "Refusing to re-record the baseline: --update refuses while a "
                f"regression stands.\n  - {detail}\n\n"
                "Fix the regression, or if the change is deliberate, say so in the "
                "ticket and re-record only once the numbers are the ones you mean."
            )
            if any("describe a different corpus" in line for line in comparison.regressions):
                msg += f"\n\nFor an intentional corpus replacement use '{CORPUS_UPDATE_COMMAND}'."
            raise RuntimeError(msg)
    else:
        comparison = Comparison(regressions=(), improvements=(), observations=(), notes=())
    write_baseline(path, current)
    return comparison


def compare(baseline: Baseline, current: Baseline) -> Comparison:
    """How *current* differs from the recorded *baseline*."""
    regressions: list[str] = []
    improvements: list[str] = []
    observations: list[str] = []
    notes: list[str] = []

    if baseline.metadata.corpus_sha256 != current.metadata.corpus_sha256:
        regressions.append(CORPUS_NONCOMPARABLE_NOTE)

    if baseline.metadata.sqlite_version != current.metadata.sqlite_version:
        notes.append(
            f"baseline recorded on SQLite {baseline.metadata.sqlite_version}, "
            f"running {current.metadata.sqlite_version} — a difference below may be "
            f"environmental rather than a real change"
        )

    skip_section_metrics = False
    if baseline.metadata.granularity == "document" and current.metadata.granularity == "section":
        skip_section_metrics = True
        notes.append(
            "granularity moved from document to section — section metrics were "
            "degenerate under document retrieval and are not comparable to the "
            "first real section numbers; document-level metrics still ratchet"
        )
    elif baseline.metadata.granularity == "section" and current.metadata.granularity == "document":
        regressions.append(
            "granularity fell from section to document — section-aware retrieval "
            "regressed; this is not a supported transition"
        )
    elif baseline.metadata.granularity != current.metadata.granularity:
        notes.append(
            f"granularity moved from {baseline.metadata.granularity} to "
            f"{current.metadata.granularity}"
        )

    skip_metrics = SECTION_METRICS if skip_section_metrics else ()
    for scope, recorded, measured in _scopes(baseline, current):
        _diff_scope(
            scope,
            recorded,
            measured,
            regressions,
            improvements,
            observations,
            skip_metrics=skip_metrics,
        )

    return Comparison(
        regressions=tuple(regressions),
        improvements=tuple(improvements),
        observations=tuple(observations),
        notes=tuple(notes),
    )


def format_failure(comparison: Comparison, *, update_command: str) -> str:
    """A message that names what moved, not merely that something did."""
    lines: list[str] = []
    if comparison.regressions:
        lines.append("Retrieval ratchet failed — retrieval got worse:")
        lines.extend(f"  - {line}" for line in comparison.regressions)
        lines.append(
            "\nRe-recording the baseline is not a way out: --update refuses while a "
            "regression stands."
        )
        if comparison.corpus_changed:
            lines.append(f"For an intentional corpus replacement use '{CORPUS_UPDATE_COMMAND}'.")
    elif comparison.improvements:
        lines.append("Retrieval ratchet: the baseline is stale — retrieval improved:")
        lines.extend(f"  - {line}" for line in comparison.improvements)
        lines.append(
            f"\nLock the improvement in with '{update_command}' and commit the "
            "baseline, so the gain cannot be lost again unnoticed."
        )
    if comparison.observations and not comparison.is_clean:
        lines.append("\nalso moved (recorded, never pass/fail):")
        lines.extend(f"  - {line}" for line in comparison.observations)
    for note in comparison.notes:
        lines.append(f"\nnote: {note}")
    return "\n".join(lines)


def _scopes(baseline: Baseline, current: Baseline) -> tuple[tuple[str, Counts, Counts], ...]:
    scopes: list[tuple[str, Counts, Counts]] = [("total", baseline.total, current.total)]
    for phrasing in PHRASINGS:
        recorded_phrasing = baseline.per_phrasing.get(phrasing)
        measured_phrasing = current.per_phrasing.get(phrasing)
        if recorded_phrasing is not None and measured_phrasing is not None:
            scopes.append((f"phrasing:{phrasing}", recorded_phrasing, measured_phrasing))
    for category in CATEGORIES:
        recorded = baseline.per_category.get(category)
        measured = current.per_category.get(category)
        if recorded is not None and measured is not None:
            scopes.append((category, recorded, measured))
    return tuple(scopes)


def _diff_scope(
    scope: str,
    recorded: Counts,
    measured: Counts,
    regressions: list[str],
    improvements: list[str],
    observations: list[str],
    *,
    skip_metrics: tuple[str, ...] = (),
) -> None:
    if recorded.queries != measured.queries:
        regressions.append(
            f"{scope}: {measured.queries} queries, baseline records {recorded.queries} — "
            f"the query set changed, so per-category counts are not comparable"
        )
        return

    was_all = recorded.as_mapping()
    now_all = measured.as_mapping()
    skipped = frozenset(skip_metrics)

    for metric in HIGHER_IS_BETTER:
        if metric in skipped:
            continue
        was, now = was_all[metric], now_all[metric]
        if now < was:
            regressions.append(
                f"{scope}: {metric} fell from {was} to {now} of {measured.queries} queries"
            )
        elif now > was:
            improvements.append(
                f"{scope}: {metric} rose from {was} to {now} of {measured.queries} queries"
            )

    for metric in OBSERVED_ONLY:
        was, now = was_all[metric], now_all[metric]
        if now != was:
            direction = "grew" if now > was else "shrank"
            observations.append(f"{scope}: {metric} {direction} from {was} to {now}")
