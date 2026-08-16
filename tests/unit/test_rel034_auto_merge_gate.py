"""Deliberate failure used once to prove auto-merge waits on required checks."""


def test_rel034_auto_merge_must_not_merge_on_red() -> None:
    assert False, "REL-034 gate: this PR must stay open while checks fail"
