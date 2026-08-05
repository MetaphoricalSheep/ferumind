"""Policy echo for propose results: the server informs; agents honor.

Propose results carry the target's resolved ``edit_policy``/``status`` plus
a ``policy_note`` (exact strings from spec-mcp §5.2). The server never
blocks on policy — the closed list of hard refusals lives in the write
service (archived targets, protected frontmatter, out-of-project paths).
"""

from __future__ import annotations

from typing import Final

from ferumind.core.documents import ParsedDocument
from ferumind.core.types import JsonObject, StrictModel

POLICY_NOTES: Final[dict[str, str]] = {
    "append": (
        "This document is append-only: only additions at the anchor or end are appropriate."
    ),
    "propose-first": (
        "This document expects curation: tell the user what will change before applying."
    ),
    "ask-human": (
        "This file is human-owned: apply only if the user explicitly requested this change "
        "in this conversation."
    ),
}

FROZEN_NOTE: Final = "Structure is frozen: additions only, no restructuring."


class PolicyEcho(StrictModel):
    edit_policy: str
    status: str
    policy_note: str | None = None


def policy_echo_for(document: ParsedDocument) -> PolicyEcho:
    """Build the policy echo for a propose result on *document*."""
    if document.status == "frozen":
        note: str | None = FROZEN_NOTE
    else:
        note = POLICY_NOTES.get(document.edit_policy)
    return PolicyEcho(
        edit_policy=document.edit_policy,
        status=document.status,
        policy_note=note,
    )


def policy_echo_json(document: ParsedDocument) -> JsonObject:
    echo = policy_echo_for(document)
    return {
        "edit_policy": echo.edit_policy,
        "status": echo.status,
        "policy_note": echo.policy_note,
    }
