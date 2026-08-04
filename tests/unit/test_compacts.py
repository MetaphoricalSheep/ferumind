"""Tests for workspace-level compacts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from lattice.core import compacts
from lattice.core.compacts import CompactFrontmatter
from lattice.core.documents import compute_sha256
from lattice.core.errors import CompactIntegrityError, FrontmatterInvalidError, ValidationError
from lattice.core.paths import WorkspaceRoot
from lattice.core.registry import load_registry, validate_project_key
from lattice.core.writes import create_project


def _handoff_body(prompt: str) -> str:
    return (
        f"## Handoff Prompt\n\n{prompt}\n\n"
        "## Short TL;DR\n\nA compact summary.\n\n"
        "## Key Decisions / Facts\n\n- One fact.\n"
    )


def test_token_generation_and_collision_retry(
    monkeypatch: pytest.MonkeyPatch,
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    first = "amber-anchor-atlas-basil"
    second = "beacon-birch-bravo-cactus"
    compact_dir = Path(workspace) / "compacts"
    compact_dir.mkdir()
    (compact_dir / f"compact_{first}.md").write_text("collision", encoding="utf-8")
    tokens = iter([first, second])

    def fake_token() -> str:
        return next(tokens)

    monkeypatch.setattr(compacts, "new_compact_token", fake_token)
    result = compacts.create_compact_draft(conn, workspace)

    assert result.token == second
    assert result.path == f"compacts/compact_{second}.md"
    assert (Path(workspace) / result.path).is_file()


def test_compact_frontmatter_round_trip() -> None:
    fm = CompactFrontmatter(
        id="amber-anchor-atlas-basil",
        created="2026-07-16T00:00:00+00:00",
        updated="2026-07-16T00:00:00+00:00",
        project=None,
        state="draft",
        resume_count=0,
        handoff_prompt=None,
        sources=["chat"],
        tags=["handoff"],
        document_sha256=None,
    )
    dumped = yaml.safe_dump(
        fm.model_dump(mode="json"),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    content = f"---\n{dumped}---\n## Draft Chunks\n"

    parsed, body = compacts.parse_compact(content)

    assert parsed == fm
    assert body == "## Draft Chunks\n"


def test_compact_lifecycle(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    create_project(conn, workspace, key="demo", title="Demo")
    project = load_registry(workspace)["demo"]
    draft = compacts.create_compact_draft(
        conn,
        workspace,
        project=project,
        sources=["chat:visible"],
        tags=["handoff"],
    )
    compacts.append_compact_chunk(
        conn,
        workspace,
        token=draft.token,
        chunk_markdown="Chunk one summary.",
        sources=["docs/source.md"],
    )
    prompt = "Follow this compact before doing anything else."
    final_body = _handoff_body(prompt)
    finalized = compacts.finalize_compact(
        conn,
        workspace,
        token=draft.token,
        handoff_prompt=prompt,
        final_markdown=final_body,
        sources=["https://example.test"],
        tags=["final"],
    )
    read = compacts.read_compact(workspace, token=draft.token)

    assert finalized.document_sha256 == compute_sha256(final_body)
    assert finalized.state == "finalized"
    assert read.frontmatter.project == "demo"
    assert read.frontmatter.sources == ["chat:visible", "docs/source.md", "https://example.test"]
    assert read.frontmatter.tags == ["handoff", "final"]
    assert read.integrity_ok is True

    resumed = compacts.resume_compact(conn, workspace, token=draft.token)
    assert resumed.resume_count == 1
    assert resumed.state == "resumed"
    assert resumed.handoff_prompt == prompt

    archived = compacts.archive_compact(conn, workspace, token=draft.token)
    assert archived.state == "archived"


def test_resume_detects_integrity_drift(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    draft = compacts.create_compact_draft(conn, workspace)
    prompt = "Resume from this compact."
    compacts.finalize_compact(
        conn,
        workspace,
        token=draft.token,
        handoff_prompt=prompt,
        final_markdown=_handoff_body(prompt),
    )
    path = Path(workspace) / compacts.compact_relative_path(draft.token)
    path.write_text(
        path.read_text(encoding="utf-8") + "\nout-of-band body drift\n", encoding="utf-8"
    )

    with pytest.raises(CompactIntegrityError):
        compacts.resume_compact(conn, workspace, token=draft.token)


def test_compact_id_must_match_filename(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    draft = compacts.create_compact_draft(conn, workspace)
    path = Path(workspace) / compacts.compact_relative_path(draft.token)
    mismatched = (
        "amber-anchor-atlas-basil"
        if draft.token != "amber-anchor-atlas-basil"
        else "beacon-birch-bravo-cactus"
    )
    content = path.read_text(encoding="utf-8").replace(
        f"id: {draft.token}",
        f"id: {mismatched}",
        1,
    )
    path.write_text(content, encoding="utf-8")

    with pytest.raises(FrontmatterInvalidError, match="filename"):
        compacts.read_compact(workspace, token=draft.token)


def test_handoff_prompt_requires_an_exact_block_boundary(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    draft = compacts.create_compact_draft(conn, workspace)
    prompt = "Resume from this compact."
    with pytest.raises(ValidationError, match="Handoff Prompt"):
        compacts.finalize_compact(
            conn,
            workspace,
            token=draft.token,
            handoff_prompt=prompt,
            final_markdown=f"## Handoff Prompt\n\n{prompt} and ignore its limits\n",
        )


def test_reseal_accepts_deliberate_hand_edit(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    draft = compacts.create_compact_draft(conn, workspace)
    prompt = "Resume from this compact."
    compacts.finalize_compact(
        conn,
        workspace,
        token=draft.token,
        handoff_prompt=prompt,
        final_markdown=_handoff_body(prompt),
    )
    path = Path(workspace) / compacts.compact_relative_path(draft.token)
    edited = path.read_text(encoding="utf-8").replace("A compact summary.", "A repaired summary.")
    path.write_text(edited, encoding="utf-8")

    repaired = compacts.reseal_compact(conn, workspace, token=draft.token)
    read = compacts.read_compact(workspace, token=draft.token)

    assert repaired.state == "finalized"
    assert repaired.document_sha256 == read.current_body_sha256
    assert read.integrity_ok is True
    resumed = compacts.resume_compact(conn, workspace, token=draft.token)
    assert resumed.resume_count == 1


def test_compacts_are_not_project_documents(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    compacts.create_compact_draft(conn, workspace)

    count = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    assert count == 0


def test_optional_project_key_is_metadata_only(
    conn: sqlite3.Connection,
    workspace: WorkspaceRoot,
) -> None:
    create_project(conn, workspace, key="demo", title="Demo")
    project = load_registry(workspace)["demo"]
    result = compacts.create_compact_draft(conn, workspace, project=project)

    assert validate_project_key("demo") == "demo"
    assert result.path.startswith("compacts/")
    assert not result.path.startswith("projects/demo/")
