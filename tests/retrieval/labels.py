"""Gold labels: the corpus's answer key.

Labels are recorded at ``(path, section_id, evidence span)`` resolution from the
first commit, even though the scorer can only *score* at document resolution
today. Section-level retrieval is the next ticket (RET-02/RET-03); a corpus
labelled by path alone would need a human to relabel every query by hand at
exactly the moment it is needed as an impartial referee. Label once, at full
resolution, and let the scorer project down to whatever the system provides.

``section_id`` uses the ids ``core.document_map.derive_sections`` produces, so
the labels speak the same vocabulary section search will. Those ids are **not**
stable across edits — renaming a heading changes one, and inserting a duplicate
heading shifts the suffixes after it — which is precisely what the corpus
integrity test is for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field

#: How a query is phrased, tracked separately from *what* it asks about.
#:
#: This axis was introduced when match semantics were strict AND (RET-01):
#: natural-language questions zeroed out whenever any function word was
#: absent. RET-05 switched to OR + bm25, so the gap narrowed, but the two
#: phrasings still fail independently and must stay separate — folding them
#: would let a collapse in one hide behind the other.
type Phrasing = Literal["natural", "keyword"]

PHRASINGS: Final[tuple[Phrasing, ...]] = ("natural", "keyword")

type Category = Literal[
    "stable-facts",
    "changed-facts",
    "procedures",
    "decisions",
    "gotchas",
    "incidents",
    "identifiers",
    "dates",
    "paraphrase",
    "false-premise",
    "buried-evidence",
    "multi-section",
    "episode",
]

#: Every category must be non-empty in the corpus. A category silently lost in
#: an edit would depress the baseline forever without anyone noticing which part
#: of retrieval stopped being measured.
CATEGORIES: Final[tuple[Category, ...]] = (
    "stable-facts",
    "changed-facts",
    "procedures",
    "decisions",
    "gotchas",
    "incidents",
    "identifiers",
    "dates",
    "paraphrase",
    "false-premise",
    "buried-evidence",
    "multi-section",
    "episode",
)

#: Maximum content stems a paraphrase query may share with its gold span. One
#: allows an unavoidable domain noun ("battery"); two starts to be a lexical
#: match with extra steps. Enforced by the corpus integrity test, never waived
#: case by case — a case that fails is re-drawn from the other independently
#: generated candidates, not hand-edited into compliance.
MAX_PARAPHRASE_OVERLAP: Final[int] = 1


class GoldAnswer(BaseModel):
    """One place the evidence answering a query actually lives."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    #: A ``derive_sections`` id within that document.
    section_id: str
    #: Text that must occur verbatim inside that section. Also the needle for
    #: the evidence-in-snippet metric.
    evidence: str


class QueryCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    category: Category
    #: ``natural`` phrases the need as a person would ask it; ``keyword`` gives
    #: the terse fragment. Both target the same gold answer, so the gap between
    #: them is a direct measure of what query phrasing costs.
    phrasing: Phrasing
    query: str
    gold: tuple[GoldAnswer, ...] = Field(min_length=1)
    #: Documents that are *also* worth surfacing but are not the gold answer.
    #: For a false-premise case this is the superseded document: an agent shown
    #: only that one confidently answers a question whose premise is false, so
    #: the harness records whether both came back.
    also_relevant: tuple[str, ...] = ()
    #: Set where the case deliberately targets archived content.
    include_archived: bool = False
    notes: str = ""

    @property
    def gold_paths(self) -> frozenset[str]:
        return frozenset(answer.path for answer in self.gold)


class QuerySet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cases: tuple[QueryCase, ...] = Field(min_length=1)

    def by_category(self, category: Category) -> tuple[QueryCase, ...]:
        return tuple(case for case in self.cases if case.category == category)

    def by_phrasing(self, phrasing: Phrasing) -> tuple[QueryCase, ...]:
        return tuple(case for case in self.cases if case.phrasing == phrasing)


def load_query_set(path: Path) -> QuerySet:
    """Load and validate ``queries.yaml``.

    Validation is strict on purpose: an unknown key is a typo that would
    otherwise be silently ignored, and a silently ignored label is a query the
    harness scores against nothing.
    """
    raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        msg = f"{path} must contain a YAML list of query cases"
        raise ValueError(msg)
    # ``isinstance`` narrows to ``list[Unknown]`` because YAML is untyped at the
    # boundary. The element type is asserted here and enforced immediately below
    # by ``model_validate``, which rejects anything that is not a valid case.
    items = cast("list[object]", raw)
    return QuerySet(cases=tuple(QueryCase.model_validate(item) for item in items))
