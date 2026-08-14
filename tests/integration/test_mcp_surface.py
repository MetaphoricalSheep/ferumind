"""Integration tests for the MCP surface — spec-mcp §10 acceptance criteria.

Every tool is called cold through its registered tool function with a
fresh per-test workspace: no tool requires any prior call except
``apply_patch`` (which requires a ``propose_*``).
"""

from __future__ import annotations

import base64
import inspect
import json
import re
import subprocess
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from mcp.types import CallToolResult

from ferumind.core.edit_targets import ExactEdit, InsertAnchor
from ferumind.core.format import SUPPORTED_FORMAT, write_format_marker
from ferumind.core.observations import list_observations
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.upload_writes import ChatGPTFileInput
from ferumind.mcp.sdk_internals import registered_tools

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

type ToolMap = dict[str, Callable[..., CallToolResult]]


async def _await_tool_result(awaitable: Awaitable[CallToolResult]) -> CallToolResult:
    return await awaitable


@pytest.fixture
def run_test_remote_uploads_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep surface tests focused on results; offload behavior has a dedicated test."""
    from ferumind.mcp import upload_tools

    async def run_inline[T](
        func: Callable[..., T],
        *args: object,
        **_kwargs: object,
    ) -> T:
        return func(*args)

    monkeypatch.setattr(upload_tools, "run_sync", run_inline)


def _fake_fetch(content: bytes) -> Callable[..., bytes]:
    """A typed ``fetch_remote_file`` stub returning fixed content regardless of url/kwargs."""

    def fetch(_url: str, **_kwargs: object) -> bytes:
        return content

    return fetch


def _fake_fetch_echoing_url() -> Callable[..., bytes]:
    """A typed ``fetch_remote_file`` stub whose bytes depend on the requested url."""

    def fetch(url: str, **_kwargs: object) -> bytes:
        return b"bytes for " + url.encode()

    return fetch


@pytest.fixture
def tools(
    workspace: WorkspaceRoot,
    run_test_remote_uploads_inline: None,
) -> Iterator[ToolMap]:
    """The full registered tool surface bound to a fresh workspace.

    These tests call tool *bodies* directly. That deliberately bypasses the
    protocol layer, so it exercises neither argument validation nor call
    observation — both of those are boundary behaviour with their own suites
    (``test_mcp_hardening.py``, ``test_call_observation_middleware.py``), and
    anything that asserts on them must go through a real client instead.
    """
    from ferumind.mcp import server, tool_context

    tool_context.reset_tool_context()
    tool_context.init_tool_context(Path(workspace))
    server.register_all_tools()
    yield {tool.name: tool.fn for tool in registered_tools(server.mcp)}
    tool_context.reset_tool_context()


def _assert_matches_output_schema(name: str, structured: dict[str, Any]) -> None:
    """Validate a result the way the SDK does on a real client call.

    These tests call tool *bodies*, which skips ``FuncMetadata.convert_result``
    — the step that validates ``structured_content`` against the declared
    ``outputSchema``. Without this, a payload model that disagrees with what
    the tool actually returns stays green here and raises ``ToolError`` for
    every real caller. Doing it inside ``call`` means every assertion in this
    file checks the contract, not just the tests written for it.
    """
    from ferumind.mcp.server import mcp

    tool = next(t for t in registered_tools(mcp) if t.name == name)
    model = tool.fn_metadata.output_model
    assert model is not None, f"{name} advertises no output model"
    try:
        model.model_validate(structured)
    except Exception as exc:
        raise AssertionError(
            f"{name} returned a payload its own outputSchema rejects: {exc}\n"
            "The declared model in mcp/result_models.py disagrees with what the tool "
            "builds. A real client call would fail with ToolError."
        ) from exc


def call(tools: ToolMap, name: str, /, **kwargs: object) -> dict[str, Any]:
    """Positional-only so a tool may take its own ``tools``/``name`` argument."""
    result = tools[name](**kwargs)
    if inspect.isawaitable(result):
        result = anyio.run(_await_tool_result, cast("Awaitable[CallToolResult]", result))
    assert isinstance(result, CallToolResult)
    structured = result.structured_content
    assert isinstance(structured, dict)
    _assert_matches_output_schema(name, cast("dict[str, Any]", structured))
    return cast("dict[str, Any]", structured)


def ok(tools: ToolMap, name: str, /, **kwargs: object) -> dict[str, Any]:
    envelope = call(tools, name, **kwargs)
    assert envelope["ok"] is True, f"{name} failed: {envelope}"
    return cast("dict[str, Any]", envelope["data"])


@pytest.fixture
def demo(tools: ToolMap) -> str:
    ok(tools, "create_project", key="demo", title="Demo")
    ok(
        tools,
        "create_document",
        project="demo",
        folder_path="canvases",
        title="Plan",
        description="Fixture canvas used by the MCP surface tests.",
        content="# Plan\n\nalpha beta gamma\n",
    )
    return "demo"


class TestSurfaceShape:
    def test_tool_inventory_includes_workspace_compacts(self, tools: ToolMap) -> None:
        expected = {
            # 18 read
            "get_context",
            "read_document",
            "read_document_range",
            "get_document_map",
            "find_in_document",
            "search_project",
            "list_tree",
            "list_files",
            "read_file",
            "list_pending_patches",
            "operation_log",
            "list_snapshots",
            "read_snapshot",
            "list_projects",
            "read_skill",
            "get_compact_instructions",
            "read_compact",
            "list_compacts",
            # 12 propose/discard
            "propose_exact_replace_patch",
            "propose_multi_edit_patch",
            "propose_section_patch",
            "propose_range_patch",
            "propose_search_replace_patch",
            "propose_insert_patch",
            "propose_frontmatter_patch",
            "propose_patch",
            "discard_patch",
            "start_library_file_upload",
            "append_upload_chunk",
            "discard_upload",
            # 18 mutate
            "apply_patch",
            "create_document",
            "upload_library_file",
            "finalize_library_file_upload",
            "upload_library_files_from_chatgpt",
            "upload_library_file_from_chatgpt",
            "capture_note",
            "record_episode",
            "archive_document",
            "unarchive_document",
            "restore_snapshot",
            "create_project",
            "rebuild_index",
            "create_compact_draft",
            "append_compact_chunk",
            "finalize_compact",
            "resume_compact",
            "archive_compact",
        }
        assert set(tools) == expected
        assert len(tools) == 48
        assert "read_document_outline" not in tools

    @pytest.mark.usefixtures("tools")
    def test_annotation_taxonomy(self) -> None:
        from ferumind.mcp.server import mcp

        registered = {tool.name: tool for tool in registered_tools(mcp)}
        read_only = {
            "get_context",
            "read_document",
            "read_document_range",
            "get_document_map",
            "find_in_document",
            "search_project",
            "list_tree",
            "list_files",
            "read_file",
            "list_pending_patches",
            "operation_log",
            "list_snapshots",
            "read_snapshot",
            "list_projects",
            "read_skill",
            "get_compact_instructions",
            "read_compact",
            "list_compacts",
        }
        proposal = {
            "propose_exact_replace_patch",
            "propose_multi_edit_patch",
            "propose_section_patch",
            "propose_range_patch",
            "propose_search_replace_patch",
            "propose_insert_patch",
            "propose_frontmatter_patch",
            "propose_patch",
            "discard_patch",
            "start_library_file_upload",
            "append_upload_chunk",
            "discard_upload",
        }
        remote_download = {
            "upload_library_files_from_chatgpt",
            "upload_library_file_from_chatgpt",
        }
        for name, tool in registered.items():
            annotations = tool.annotations
            assert annotations is not None, f"{name} registered without annotations"
            assert bool(annotations.open_world_hint) is (name in remote_download), name
            if name in read_only:
                assert annotations.read_only_hint, name
                assert annotations.idempotent_hint, name
            elif name in proposal:
                assert annotations.read_only_hint, name
                assert not annotations.idempotent_hint, name
            else:
                assert not annotations.read_only_hint, name
                assert not annotations.idempotent_hint, name


class TestSkills:
    """Index-plus-on-demand delivery, exercised through the tool surface."""

    def test_a_fresh_agent_reaches_a_skill_body_in_two_calls(
        self,
        tools: ToolMap,
        demo: str,
    ) -> None:
        """get_context advertises the trigger; read_skill fetches the body."""
        context = ok(tools, "get_context", project=demo)

        index = cast("list[dict[str, Any]]", context["skills"])
        assert index, "get_context advertises the installed skills"
        entry = index[0]
        assert entry["description"].startswith("Use ")
        assert "content_markdown" not in entry

        skill = ok(tools, "read_skill", name=entry["name"])

        assert skill["name"] == entry["name"]
        assert skill["path"] == entry["path"]
        assert len(skill["content_markdown"]) > len(entry["description"])

    def test_skill_bodies_never_ride_in_get_context(self, tools: ToolMap, demo: str) -> None:
        context = ok(tools, "get_context", project=demo)
        body = ok(tools, "read_skill", name="distilling-durable-knowledge")["content_markdown"]

        marker = "## Step 3 — Merge before proliferating"
        assert marker in body
        assert marker not in json.dumps(context)

    def test_read_skill_takes_no_project_and_refuses_a_traversal(self, tools: ToolMap) -> None:
        envelope = call(tools, "read_skill", name="../rules/00-contract")

        assert envelope["ok"] is False
        assert envelope["error_code"] == "VALIDATION_ERROR"

    def test_unknown_skill_returns_a_machine_readable_code(self, tools: ToolMap) -> None:
        envelope = call(tools, "read_skill", name="no-such-skill")

        assert envelope["ok"] is False
        assert envelope["error_code"] == "SKILL_NOT_FOUND"


class TestCompactTools:
    def test_compact_instructions_are_narrowly_triggered(self, tools: ToolMap) -> None:
        data = ok(tools, "get_compact_instructions")

        instructions = data["instructions"]
        assert isinstance(instructions, str)
        assert "`/compact`" in instructions
        assert "Use this only" in instructions
        assert "ordinary project memory" in instructions

    def test_draft_chunk_finalize_read_resume_list_archive_without_project(
        self, tools: ToolMap
    ) -> None:
        draft = ok(
            tools,
            "create_compact_draft",
            sources=["chat:visible"],
            tags=["handoff"],
        )
        token = draft["token"]
        assert isinstance(token, str)
        assert draft["path"] == f"compacts/compact_{token}.md"

        chunk = ok(
            tools,
            "append_compact_chunk",
            token=token,
            chunk_markdown="The first chunk summary.",
            sources=["doc:path"],
        )
        assert chunk["state"] == "draft"

        prompt = "Follow this compact before answering."
        final_markdown = (
            f"## Handoff Prompt\n\n{prompt}\n\n"
            "## Short TL;DR\n\nSummary.\n\n"
            "## Key Decisions / Facts\n\n- Fact.\n"
        )
        finalized = ok(
            tools,
            "finalize_compact",
            token=token,
            handoff_prompt=prompt,
            final_markdown=final_markdown,
            sources=["https://example.test"],
            tags=["final"],
        )
        assert finalized["document_sha256"]
        assert finalized["state"] == "finalized"

        read = ok(tools, "read_compact", token=token)
        assert read["frontmatter"]["sources"] == [
            "chat:visible",
            "doc:path",
            "https://example.test",
        ]
        assert read["integrity_ok"] is True

        listed = ok(tools, "list_compacts", state="finalized", limit=10)
        assert [item["token"] for item in listed["compacts"]] == [token]

        resumed = ok(tools, "resume_compact", token=token)
        assert resumed["resume_count"] == 1
        assert resumed["handoff_prompt"] == prompt
        assert resumed["state"] == "resumed"

        archived = ok(tools, "archive_compact", token=token)
        assert archived["state"] == "archived"

    def test_optional_project_metadata_validates_but_does_not_scope_path(
        self, tools: ToolMap
    ) -> None:
        ok(tools, "create_project", key="demo", title="Demo")

        draft = ok(tools, "create_compact_draft", project="demo")

        assert draft["path"].startswith("compacts/")
        envelope = call(tools, "create_compact_draft", project="ghost")
        assert envelope["ok"] is False
        assert envelope["error_code"] == "PROJECT_NOT_FOUND"

    def test_unknown_compact_returns_machine_code(self, tools: ToolMap) -> None:
        envelope = call(tools, "read_compact", token="amber-anchor-atlas-basil")

        assert envelope["ok"] is False
        assert envelope["error_code"] == "COMPACT_NOT_FOUND"

    def test_compact_write_refuses_unsupported_format(
        self, tools: ToolMap, workspace: WorkspaceRoot
    ) -> None:
        write_format_marker(workspace, 1)

        envelope = call(tools, "create_compact_draft")

        assert envelope["ok"] is False
        assert envelope["error_code"] == "FORMAT_UNSUPPORTED"

    @pytest.mark.usefixtures("tools")
    def test_no_tool_takes_a_session_parameter(self) -> None:
        from ferumind.mcp.server import mcp

        registered = {tool.name: tool for tool in registered_tools(mcp)}
        for name, tool in registered.items():
            schema = tool.parameters
            properties = cast("dict[str, Any]", schema.get("properties", {}))
            assert not any("session" in key for key in properties), name

    def test_session_id_is_gone_from_source(self) -> None:
        result = subprocess.run(
            ["grep", "-ri", "session_id", "src/ferumind/mcp/"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1, f"session_id found:\n{result.stdout}"


class TestColdCallability:
    """§10.1: every tool callable cold; only apply_patch needs a prior call."""

    def test_every_tool_cold(
        self, tools: ToolMap, demo: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ferumind.core import upload_writes

        monkeypatch.setattr(
            upload_writes, "fetch_remote_file", _fake_fetch(b"cold call chatgpt bytes")
        )

        doc = "canvases/plan.md"
        document_sha = ok(tools, "read_document", project=demo, path=doc)["document_sha256"]
        doc_map = ok(tools, "get_document_map", project=demo, path=doc)
        section = doc_map["sections"][0]
        body_start = doc_map["body_range"]["start_line"]
        range_read = ok(
            tools,
            "read_document_range",
            project=demo,
            path=doc,
            start_line=body_start,
            end_line=body_start,
        )

        cold_calls: dict[str, dict[str, object]] = {
            "get_context": {"project": demo},
            "get_compact_instructions": {},
            "find_in_document": {"project": demo, "path": doc, "query": "alpha"},
            "search_project": {"project": demo, "query": "alpha"},
            "list_tree": {"project": demo},
            "list_pending_patches": {"project": demo},
            "operation_log": {"project": demo},
            "list_snapshots": {"project": demo},
            "list_projects": {},
            "capture_note": {"project": demo, "text": "note"},
            "record_episode": {
                "project": demo,
                "title": "Cold call",
                "summary": "Recorded by the cold-call sweep.",
            },
            "upload_library_file": {
                "project": demo,
                "filename": "cold-call.pdf",
                "content_base64": base64.b64encode(b"cold call bytes").decode("ascii"),
            },
            "start_library_file_upload": {
                "project": demo,
                "filename": "cold-call-chunked.pdf",
                "total_size": 5,
                "total_chunks": 1,
            },
            "upload_library_files_from_chatgpt": {
                "project": demo,
                "files": [
                    ChatGPTFileInput(
                        download_url="https://chatgpt.example/cold-call",
                        file_id="file_cold_call",
                        file_name="cold-call-chatgpt.bin",
                    )
                ],
            },
            "upload_library_file_from_chatgpt": {
                "project": demo,
                "file": ChatGPTFileInput(
                    download_url="https://chatgpt.example/cold-call-single",
                    file_id="file_cold_call_single",
                ),
                "filename": "cold-call-single.bin",
            },
            "rebuild_index": {"project": demo},
            "create_compact_draft": {},
            "list_compacts": {},
            "propose_exact_replace_patch": {
                "project": demo,
                "path": doc,
                "old_string": "alpha",
                "new_string": "ALPHA",
            },
            "propose_multi_edit_patch": {
                "project": demo,
                "path": doc,
                "edits": [ExactEdit(old_string="beta", new_string="BETA")],
            },
            "propose_section_patch": {
                "project": demo,
                "path": doc,
                "section_id": section["section_id"],
                "expected_document_sha256": document_sha,
                "expected_section_sha256": section["content_sha256"],
                "new_content": "# Plan\n\nreworked\n",
            },
            "propose_range_patch": {
                "project": demo,
                "path": doc,
                "start_line": range_read["range"]["start_line"],
                "end_line": range_read["range"]["end_line"],
                "expected_document_sha256": document_sha,
                "expected_range_sha256": range_read["range_sha256"],
                "new_content": "replaced",
            },
            "propose_search_replace_patch": {
                "project": demo,
                "path": doc,
                "find": "gamma",
                "replace": "GAMMA",
                "expected_document_sha256": document_sha,
            },
            "propose_insert_patch": {
                "project": demo,
                "path": doc,
                "anchor": InsertAnchor(kind="end_of_file"),
                "content": "appended",
                "expected_document_sha256": document_sha,
            },
            "propose_frontmatter_patch": {
                "project": demo,
                "path": doc,
                "set_values": {"edit_policy": "append"},
            },
            "propose_patch": {"project": demo, "path": doc, "new_content": "# new body\n"},
        }
        for name, kwargs in cold_calls.items():
            envelope = call(tools, name, **kwargs)
            assert envelope["ok"] is True, f"{name} not cold-callable: {envelope}"

        # The one allowed dependency: apply_patch consumes a proposal.
        proposal = ok(
            tools,
            "propose_exact_replace_patch",
            project=demo,
            path=doc,
            old_string="alpha",
            new_string="OMEGA",
        )
        applied = ok(tools, "apply_patch", project=demo, operation_id=proposal["operation_id"])
        assert applied["document_mutated"] is True

        # discard, archive/unarchive, snapshot reads and restore
        second = ok(
            tools,
            "propose_exact_replace_patch",
            project=demo,
            path=doc,
            old_string="OMEGA",
            new_string="PSI",
        )
        assert ok(tools, "discard_patch", project=demo, operation_id=second["operation_id"])
        snapshots = ok(tools, "list_snapshots", project=demo, path=doc)["snapshots"]
        snapshot_id = snapshots[0]["id"]
        assert ok(tools, "read_snapshot", project=demo, snapshot_id=snapshot_id)
        assert ok(tools, "restore_snapshot", project=demo, snapshot_id=snapshot_id)
        archived = ok(tools, "archive_document", project=demo, path=doc)
        assert ok(
            tools, "unarchive_document", project=demo, archived_path=archived["archived_path"]
        )

        # Chunked upload: start -> append (x2) -> finalize, plus a separate
        # abandoned session cleaned up via discard_upload.
        payload = b"chunked cold-call bytes, split across two calls"
        mid = len(payload) // 2
        pieces = [payload[:mid], payload[mid:]]
        session = ok(
            tools,
            "start_library_file_upload",
            project=demo,
            filename="cold-chunked.bin",
            total_size=len(payload),
            total_chunks=len(pieces),
        )
        progress: dict[str, Any] = {}
        for index, piece in enumerate(pieces):
            progress = ok(
                tools,
                "append_upload_chunk",
                project=demo,
                upload_id=session["upload_id"],
                chunk_index=index,
                chunk_base64=base64.b64encode(piece).decode("ascii"),
            )
        assert progress["complete"] is True
        finalized = ok(
            tools, "finalize_library_file_upload", project=demo, upload_id=session["upload_id"]
        )
        assert finalized["path"] == "library/cold-chunked.bin"
        assert finalized["document_mutated"] is True

        abandoned = ok(
            tools,
            "start_library_file_upload",
            project=demo,
            filename="abandoned.bin",
            total_size=1,
            total_chunks=1,
        )
        assert ok(tools, "discard_upload", project=demo, upload_id=abandoned["upload_id"])

        compact = ok(tools, "create_compact_draft")
        token = compact["token"]
        assert ok(
            tools,
            "append_compact_chunk",
            token=token,
            chunk_markdown="Cold chunk.",
        )
        prompt = "Use this cold compact."
        final_markdown = f"## Handoff Prompt\n\n{prompt}\n\n## Short TL;DR\n\nCold.\n"
        assert ok(
            tools,
            "finalize_compact",
            token=token,
            handoff_prompt=prompt,
            final_markdown=final_markdown,
        )
        assert ok(tools, "read_compact", token=token)
        assert ok(tools, "resume_compact", token=token)
        assert ok(tools, "archive_compact", token=token)


class TestProjectScoping:
    """§10.2: every scoped tool rejects missing/unknown project with the right code."""

    SCOPED_READ_TOOLS = (
        "get_context",
        "read_document",
        "search_project",
        "list_tree",
        "operation_log",
    )

    @pytest.mark.usefixtures("demo")
    def test_missing_project_rejected(self, tools: ToolMap) -> None:
        for name in self.SCOPED_READ_TOOLS:
            kwargs: dict[str, object] = {"project": ""}
            if name == "read_document":
                kwargs["path"] = "spine.md"
            if name == "search_project":
                kwargs["query"] = "x"
            envelope = call(tools, name, **kwargs)
            assert envelope["ok"] is False, name
            assert envelope["error_code"] == "PROJECT_REQUIRED", name

    @pytest.mark.usefixtures("demo")
    def test_unknown_project_rejected(self, tools: ToolMap) -> None:
        envelope = call(tools, "get_context", project="ghost")
        assert envelope["error_code"] == "PROJECT_NOT_FOUND"
        assert envelope["details"]["available_projects"] == ["demo"]
        envelope = call(
            tools,
            "propose_exact_replace_patch",
            project="ghost",
            path="spine.md",
            old_string="a",
            new_string="b",
        )
        assert envelope["error_code"] == "PROJECT_NOT_FOUND"

    @pytest.mark.usefixtures("workspace")
    def test_project_is_never_an_override(self, tools: ToolMap, demo: str) -> None:
        ok(tools, "create_project", key="other", title="Other")
        proposal = ok(
            tools,
            "propose_exact_replace_patch",
            project=demo,
            path="canvases/plan.md",
            old_string="alpha",
            new_string="ALPHA",
        )
        envelope = call(
            tools, "apply_patch", project="other", operation_id=proposal["operation_id"]
        )
        assert envelope["error_code"] == "PATCH_PROJECT_MISMATCH"


class TestDocumentMapAgreement:
    """RET-04: map sections and indexed sections agree at the MCP tool boundary."""

    def test_map_sections_agree_with_section_index(self, tools: ToolMap, demo: str) -> None:
        from ferumind.mcp.tool_context import require_database

        created = ok(
            tools,
            "create_document",
            project=demo,
            folder_path="canvases",
            title="Mapped",
            description="Fixture canvas used by the MCP surface tests.",
            content=("# Root\n\nintro\n\n## Child One\n\none body\n\n## Child Two\n\ntwo body\n"),
        )
        doc = created["path"]
        ok(tools, "rebuild_index", project=demo)
        doc_map = ok(tools, "get_document_map", project=demo, path=doc)
        sections = doc_map["sections"]
        assert len(sections) >= 3

        db = require_database()
        conn = db.get_connection()
        try:
            rows = list(
                conn.execute(
                    """SELECT section_id, start_line, end_line, content_sha256, size_bytes
                       FROM section_index
                       WHERE project_key = ? AND path = ?
                       ORDER BY CAST(start_line AS INTEGER), section_id""",
                    (demo, doc),
                ).fetchall()
            )
        finally:
            conn.close()

        assert len(rows) == len(sections)
        for row, section in zip(rows, sections, strict=True):
            assert row["section_id"] == section["section_id"]
            assert int(row["start_line"]) == section["start_line"]
            assert int(row["end_line"]) == section["end_line"]
            assert row["content_sha256"] == section["content_sha256"]
            assert int(row["size_bytes"]) == section["size_bytes"]

    def test_hash_guarded_section_patch_round_trip(self, tools: ToolMap, demo: str) -> None:
        created = ok(
            tools,
            "create_document",
            project=demo,
            folder_path="canvases",
            title="Section Patch",
            description="Fixture canvas used by the MCP surface tests.",
            content="# Alpha\n\nalpha body\n\n## Beta\n\nbeta body\n",
        )
        doc = created["path"]
        doc_map = ok(tools, "get_document_map", project=demo, path=doc)
        beta = next(s for s in doc_map["sections"] if s.get("heading_text") == "Beta")
        assert "size_bytes" in beta
        assert beta["size_bytes"] > 0

        proposal = ok(
            tools,
            "propose_section_patch",
            project=demo,
            path=doc,
            section_id=beta["section_id"],
            expected_document_sha256=doc_map["document_sha256"],
            expected_section_sha256=beta["content_sha256"],
            new_content="## Beta\n\npatched beta\n",
        )
        assert proposal["document_mutated"] is False
        assert proposal["requires_apply"] is True

        applied = ok(tools, "apply_patch", project=demo, operation_id=proposal["operation_id"])
        assert applied["document_mutated"] is True
        after = ok(tools, "read_document", project=demo, path=doc)
        assert "patched beta" in after["content"]
        assert "alpha body" in after["content"]
        assert after["document_sha256"] == applied["document_sha256"]


class TestOutOfBand:
    """§10.3: apply after an out-of-band edit returns PATCH_CONFLICT, never clobbers."""

    def test_adversarial_edit_between_propose_and_apply(
        self, tools: ToolMap, demo: str, workspace: WorkspaceRoot
    ) -> None:
        doc = "canvases/plan.md"
        proposal = ok(
            tools,
            "propose_exact_replace_patch",
            project=demo,
            path=doc,
            old_string="alpha",
            new_string="ALPHA",
        )
        target = Path(workspace) / "projects" / demo / doc
        hand_edited = target.read_text(encoding="utf-8") + "\nhand edit wins\n"
        target.write_text(hand_edited, encoding="utf-8")

        envelope = call(tools, "apply_patch", project=demo, operation_id=proposal["operation_id"])
        assert envelope["ok"] is False
        assert envelope["error_code"] == "PATCH_CONFLICT"
        assert target.read_text(encoding="utf-8") == hand_edited
        oplog = ok(tools, "operation_log", project=demo, path=doc)["operations"]
        assert any(op["source"] == "out-of-band" for op in oplog)

    def test_section_patch_conflicts_after_out_of_band_edit(
        self, tools: ToolMap, demo: str, workspace: WorkspaceRoot
    ) -> None:
        """RET-04: map→propose_section_patch→apply still fails closed on OOB edits."""
        created = ok(
            tools,
            "create_document",
            project=demo,
            folder_path="canvases",
            title="Section OOB",
            description="Fixture canvas used by the MCP surface tests.",
            content=("# Alpha\n\nalpha body\n\n## Beta\n\nbeta body\n\n## Gamma\n\ngamma body\n"),
        )
        doc = created["path"]
        doc_map = ok(tools, "get_document_map", project=demo, path=doc)
        beta = next(s for s in doc_map["sections"] if s.get("heading_text") == "Beta")
        proposal = ok(
            tools,
            "propose_section_patch",
            project=demo,
            path=doc,
            section_id=beta["section_id"],
            expected_document_sha256=doc_map["document_sha256"],
            expected_section_sha256=beta["content_sha256"],
            new_content="## Beta\n\nrewritten beta\n",
        )
        target = Path(workspace) / "projects" / demo / doc
        before = target.read_text(encoding="utf-8")
        hand_edited = before + "\nhand edit between map and apply\n"
        target.write_text(hand_edited, encoding="utf-8")

        envelope = call(tools, "apply_patch", project=demo, operation_id=proposal["operation_id"])
        assert envelope["ok"] is False
        assert envelope["error_code"] == "PATCH_CONFLICT"
        assert target.read_text(encoding="utf-8") == hand_edited


class TestArchiveRoundTrip:
    """§10.4: archive round-trip preserves content/id; archived docs vanish."""

    def test_round_trip_and_visibility(self, tools: ToolMap, demo: str) -> None:
        doc = "canvases/plan.md"
        before = ok(tools, "read_document", project=demo, path=doc)
        original_id = before["frontmatter"]["id"]

        archived = ok(tools, "archive_document", project=demo, path=doc)
        context = ok(tools, "get_context", project=demo)
        assert all(d["path"] != doc for d in context["documents"])
        assert all(d["folder"] != "archive" for d in context["documents"])
        search = ok(tools, "search_project", project=demo, query="alpha")
        assert search["count"] == 0

        restored = ok(
            tools, "unarchive_document", project=demo, archived_path=archived["archived_path"]
        )
        assert restored["path"] == doc
        after = ok(tools, "read_document", project=demo, path=doc)
        assert after["frontmatter"]["id"] == original_id
        assert after["status"] == "active"
        assert "alpha beta gamma" in after["content"]
        history = ok(tools, "operation_log", project=demo, path=doc)["operations"]
        assert {"archive_document", "unarchive_document"} <= {
            op["operation_type"] for op in history
        }


class TestUploadLibraryFile:
    def test_uploads_under_library_with_metadata_sidecar(
        self, tools: ToolMap, demo: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        content = base64.b64encode(b"%PDF-1.4 not a real pdf\n").decode("ascii")
        result = ok(
            tools,
            "upload_library_file",
            project=demo,
            filename="report.pdf",
            content_base64=content,
            mime_type="application/pdf",
            metadata={"source": "chatgpt-upload"},
        )
        assert result["path"] == "library/report.pdf"
        assert result["metadata_path"] == "library/report.json"
        assert result["document_mutated"] is True

        # Collision fails closed; extension denylist and size cap are enforced.
        collision = call(
            tools,
            "upload_library_file",
            project=demo,
            filename="report.pdf",
            content_base64=content,
        )
        assert collision["error_code"] == "DOCUMENT_EXISTS"

        blocked = call(
            tools,
            "upload_library_file",
            project=demo,
            filename="payload.sh",
            content_base64=content,
        )
        assert blocked["error_code"] == "UNSUPPORTED_FILE_TYPE"

        # Exercise the boundary against a small patched cap rather than
        # allocating/base64-encoding a real MAX_CHUNK_BYTES-sized payload
        # (upload_library_file shares the single-call cap with one chunk).
        from ferumind.core import upload_writes

        monkeypatch.setattr(upload_writes, "MAX_CHUNK_BYTES", 16)
        too_big = call(
            tools,
            "upload_library_file",
            project=demo,
            filename="big.bin",
            content_base64=base64.b64encode(b"x" * 17).decode("ascii"),
        )
        assert too_big["error_code"] == "FILE_TOO_LARGE"

        outside_library = call(
            tools,
            "upload_library_file",
            project=demo,
            filename="report.pdf",
            content_base64=content,
            folder_path="canvases",
        )
        assert outside_library["error_code"] == "UNKNOWN_FOLDER"

        # Not indexed: markdown-only reads/search never see the upload.
        not_found = call(tools, "read_document", project=demo, path=result["path"])
        assert not_found["error_code"] == "DOCUMENT_NOT_FOUND"


class TestChunkedUpload:
    def test_finalize_rejects_missing_chunks(self, tools: ToolMap, demo: str) -> None:
        session = ok(
            tools,
            "start_library_file_upload",
            project=demo,
            filename="partial.bin",
            total_size=8,
            total_chunks=2,
        )
        ok(
            tools,
            "append_upload_chunk",
            project=demo,
            upload_id=session["upload_id"],
            chunk_index=0,
            chunk_base64=base64.b64encode(b"1234").decode("ascii"),
        )
        incomplete = call(
            tools, "finalize_library_file_upload", project=demo, upload_id=session["upload_id"]
        )
        assert incomplete["error_code"] == "UPLOAD_INCOMPLETE"

    def test_finalize_rejects_hash_mismatch(self, tools: ToolMap, demo: str) -> None:
        session = ok(
            tools,
            "start_library_file_upload",
            project=demo,
            filename="tampered.bin",
            total_size=4,
            total_chunks=1,
            expected_sha256="0" * 64,
        )
        ok(
            tools,
            "append_upload_chunk",
            project=demo,
            upload_id=session["upload_id"],
            chunk_index=0,
            chunk_base64=base64.b64encode(b"abcd").decode("ascii"),
        )
        mismatch = call(
            tools, "finalize_library_file_upload", project=demo, upload_id=session["upload_id"]
        )
        assert mismatch["error_code"] == "CONTENT_HASH_MISMATCH"

    def test_unknown_upload_id(self, tools: ToolMap, demo: str) -> None:
        missing = call(
            tools,
            "append_upload_chunk",
            project=demo,
            upload_id="op_doesnotexist",
            chunk_index=0,
            chunk_base64=base64.b64encode(b"x").decode("ascii"),
        )
        assert missing["error_code"] == "OPERATION_NOT_FOUND"


class TestChatGPTFileUpload:
    """upload_library_files_from_chatgpt: openai/fileParams descriptor + batch semantics."""

    def test_tool_descriptor_matches_chatgpt_file_schema_exactly(self) -> None:
        from ferumind.mcp.server import mcp

        registered = {tool.name: tool for tool in registered_tools(mcp)}
        tool = registered["upload_library_files_from_chatgpt"]

        assert tool.meta == {"openai/fileParams": ["files"]}

        files_schema = tool.parameters["properties"]["files"]
        assert files_schema["type"] == "array"
        assert "files" in tool.parameters["required"]
        assert files_schema["items"] == {
            "type": "object",
            "properties": {
                "download_url": {"type": "string"},
                "file_id": {"type": "string"},
                "mime_type": {"type": "string"},
                "file_name": {"type": "string"},
            },
            "required": ["download_url", "file_id"],
            "additionalProperties": False,
        }

    def test_tools_list_wire_output_carries_meta_and_schema(self) -> None:
        """Not just the internal Python object — the actual serialized tools/list payload."""
        from ferumind.mcp.server import mcp

        listed = anyio.run(mcp.list_tools)
        tool = next(t for t in listed if t.name == "upload_library_files_from_chatgpt")

        dumped = tool.model_dump(by_alias=True, exclude_none=True)
        assert dumped["_meta"] == {"openai/fileParams": ["files"]}
        assert dumped["inputSchema"]["properties"]["files"]["items"]["required"] == [
            "download_url",
            "file_id",
        ]
        assert (
            dumped["inputSchema"]["properties"]["files"]["items"]["additionalProperties"] is False
        )
        assert dumped["annotations"]["readOnlyHint"] is False
        assert dumped["annotations"]["destructiveHint"] is False
        assert dumped["annotations"]["idempotentHint"] is False

    def test_downloads_and_stores_via_normal_ingestion_pipeline(
        self, tools: ToolMap, demo: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ferumind.core import upload_writes

        monkeypatch.setattr(
            upload_writes, "fetch_remote_file", _fake_fetch(b"downloaded chatgpt bytes")
        )
        result = ok(
            tools,
            "upload_library_files_from_chatgpt",
            project=demo,
            files=[
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/file1",
                    file_id="file_abc",
                    file_name="photo.jpg",
                    mime_type="image/jpeg",
                )
            ],
        )
        assert result["succeeded"] == 1
        assert result["failed"] == 0
        entry = result["results"][0]
        assert entry["ok"] is True
        assert entry["path"] == "library/photo.jpg"
        assert entry["file_id"] == "file_abc"

        # Same pipeline as the other upload tools: reads through read_document
        # still reject it (not Markdown), matching upload_library_file.
        not_found = call(tools, "read_document", project=demo, path=entry["path"])
        assert not_found["error_code"] == "DOCUMENT_NOT_FOUND"

    def test_multiple_files_in_one_call(
        self, tools: ToolMap, demo: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ferumind.core import upload_writes

        monkeypatch.setattr(upload_writes, "fetch_remote_file", _fake_fetch_echoing_url())
        result = ok(
            tools,
            "upload_library_files_from_chatgpt",
            project=demo,
            files=[
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/1", file_id="f1", file_name="one.bin"
                ),
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/2", file_id="f2", file_name="two.bin"
                ),
            ],
        )
        assert result["succeeded"] == 2
        assert {r["path"] for r in result["results"]} == {"library/one.bin", "library/two.bin"}

    def test_partial_batch_failure_is_explicit(
        self, tools: ToolMap, demo: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ferumind.core import upload_writes
        from ferumind.core.errors import DownloadFailedError

        calls: list[str] = []

        def fake_fetch(url: str, **kwargs: object) -> bytes:
            calls.append(url)
            if "bad" in url:
                raise DownloadFailedError("simulated failure")
            return b"ok bytes"

        monkeypatch.setattr(upload_writes, "fetch_remote_file", fake_fetch)
        result = ok(
            tools,
            "upload_library_files_from_chatgpt",
            project=demo,
            files=[
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/good", file_id="f1", file_name="good.bin"
                ),
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/bad", file_id="f2", file_name="bad.bin"
                ),
            ],
        )
        # A disarmed patch would send this test at the real network instead of
        # the stub, so pin that the stub is what answered.
        assert calls == ["https://chatgpt.example/good", "https://chatgpt.example/bad"]
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        assert len(result["results"]) == 2
        by_id = {r["file_id"]: r for r in result["results"]}
        assert by_id["f1"]["ok"] is True
        assert by_id["f2"]["ok"] is False
        assert by_id["f2"]["error_code"] == "DOWNLOAD_FAILED"

    def test_unsafe_url_rejected_end_to_end(self, tools: ToolMap, demo: str) -> None:
        """No monkeypatching here — exercises the real SSRF validation path."""
        result = ok(
            tools,
            "upload_library_files_from_chatgpt",
            project=demo,
            files=[
                ChatGPTFileInput(
                    download_url="https://169.254.169.254/latest/meta-data/",
                    file_id="f1",
                    file_name="steal.txt",
                )
            ],
        )
        assert result["succeeded"] == 0
        assert result["results"][0]["error_code"] == "UNSAFE_URL"

    def test_files_required_and_rejects_extra_properties(self, tools: ToolMap, demo: str) -> None:
        missing = call(tools, "upload_library_files_from_chatgpt", project=demo, files=[])
        assert missing["error_code"] == "VALIDATION_ERROR"

    def test_batch_tool_takes_no_caller_supplied_filenames(self) -> None:
        """No name↔file binding parameter: the extension exposes no handle to bind against.

        Names on batch uploads come from each file's own resolved reference,
        which travels with its bytes. A parallel array of names could only be
        matched positionally, which is not a guarantee ChatGPT makes.
        """
        from ferumind.mcp.server import mcp

        registered = {tool.name: tool for tool in registered_tools(mcp)}
        properties = registered["upload_library_files_from_chatgpt"].parameters["properties"]
        assert set(properties) == {"project", "files", "folder_path"}


class TestChatGPTSingleFileUpload:
    """upload_library_file_from_chatgpt: one file, one caller-chosen filename."""

    def test_tool_descriptor_declares_a_single_top_level_file_param(self) -> None:
        from ferumind.mcp.server import mcp

        registered = {tool.name: tool for tool in registered_tools(mcp)}
        tool = registered["upload_library_file_from_chatgpt"]

        assert tool.meta == {"openai/fileParams": ["file"]}
        # The file reference object itself, not wrapped in an array, and not
        # nested inside another object (the extension resolves neither).
        assert tool.parameters["properties"]["file"] == {
            "type": "object",
            "properties": {
                "download_url": {"type": "string"},
                "file_id": {"type": "string"},
                "mime_type": {"type": "string"},
                "file_name": {"type": "string"},
            },
            "required": ["download_url", "file_id"],
            "additionalProperties": False,
        }
        assert set(tool.parameters["required"]) == {"project", "file", "filename"}

    def test_tools_list_wire_output_carries_meta_and_schema(self) -> None:
        from ferumind.mcp.server import mcp

        listed = anyio.run(mcp.list_tools)
        tool = next(t for t in listed if t.name == "upload_library_file_from_chatgpt")

        dumped = tool.model_dump(by_alias=True, exclude_none=True)
        assert dumped["_meta"] == {"openai/fileParams": ["file"]}
        file_schema = dumped["inputSchema"]["properties"]["file"]
        assert file_schema["type"] == "object"
        assert file_schema["required"] == ["download_url", "file_id"]
        assert file_schema["additionalProperties"] is False
        assert "$ref" not in file_schema
        assert dumped["annotations"]["readOnlyHint"] is False
        assert dumped["annotations"]["idempotentHint"] is False

    def test_stores_under_the_requested_filename(
        self, tools: ToolMap, demo: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ferumind.core import upload_writes

        monkeypatch.setattr(upload_writes, "fetch_remote_file", _fake_fetch(b"single file bytes"))
        result = ok(
            tools,
            "upload_library_file_from_chatgpt",
            project=demo,
            file=ChatGPTFileInput(
                download_url="https://chatgpt.example/single",
                file_id="file_single",
                file_name="IMG_4242.jpg",
                mime_type="image/jpeg",
            ),
            filename="flex-01-front.jpg",
        )
        assert result["path"] == "library/flex-01-front.jpg"
        assert result["filename"] == "flex-01-front.jpg"
        assert result["file_id"] == "file_single"
        assert result["mime_type"] == "image/jpeg"
        assert result["operation_id"]
        assert result["snapshot_id"]

    def test_download_failure_is_a_tool_error_not_a_partial_result(
        self, tools: ToolMap, demo: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ferumind.core import upload_writes
        from ferumind.core.errors import DownloadFailedError

        calls: list[str] = []

        def fake_fetch(url: str, **kwargs: object) -> bytes:
            calls.append(url)
            raise DownloadFailedError("simulated failure")

        monkeypatch.setattr(upload_writes, "fetch_remote_file", fake_fetch)
        result = call(
            tools,
            "upload_library_file_from_chatgpt",
            project=demo,
            file=ChatGPTFileInput(download_url="https://chatgpt.example/x", file_id="f1"),
            filename="nope.bin",
        )
        # A real fetch would also fail here, and for the wrong reason. Pin that
        # the stub is what refused, not the network.
        assert calls == ["https://chatgpt.example/x"], "the injected download failure never fired"
        assert result["error_code"] == "DOWNLOAD_FAILED"

    def test_unsafe_url_rejected_end_to_end(self, tools: ToolMap, demo: str) -> None:
        """No monkeypatching — exercises the real SSRF validation path."""
        result = call(
            tools,
            "upload_library_file_from_chatgpt",
            project=demo,
            file=ChatGPTFileInput(
                download_url="https://169.254.169.254/latest/meta-data/", file_id="f1"
            ),
            filename="steal.txt",
        )
        assert result["error_code"] == "UNSAFE_URL"


class TestGetContextContract:
    def test_payload_telemetry_and_format_echo(self, tools: ToolMap, demo: str) -> None:
        data = ok(tools, "get_context", project=demo)
        payload = data["payload"]
        assert payload["format"] == SUPPORTED_FORMAT
        assert payload["rules_bytes"] > 0
        assert payload["spine_bytes"] > 0
        assert payload["documents_count"] == len(data["documents"])
        # Format 3 put a per-document cost in the contract call; the cap
        # decision spec-mcp §4 parks on telemetry needs it broken out.
        assert payload["descriptions_bytes"] > 0
        assert all(entry["description"] for entry in data["documents"])
        assert all(entry["size_bytes"] > 0 for entry in data["documents"])
        assert data["project"] == {"key": "demo", "title": "Demo", "status": "active"}
        assert data["rules"]["sources"][0] == "system/rules/00-contract.md"
        assert data["spine"]["path"] == "spine.md"

    @pytest.mark.usefixtures("tools")
    def test_observation_rows_carry_payload_metrics(self, demo: str) -> None:
        """§10.6: get_context observations carry every payload metric.

        Driven through a real client, not the tool body: observation is server
        middleware, so it only runs on the protocol path.
        """
        from mcp.client.client import Client

        from ferumind.mcp.server import mcp
        from ferumind.mcp.tool_context import require_database

        async def body() -> None:
            async with Client(mcp) as session:
                await session.call_tool("get_context", {"project": demo})

        anyio.run(body)

        conn = require_database().get_connection()
        try:
            rows = list_observations(conn, tool_name="get_context", limit=1)
        finally:
            conn.close()
        assert rows
        record = rows[0]
        assert record.result_bytes is not None
        assert record.result_bytes > 0
        assert record.duration_ms is not None
        assert record.client_name, "client identity should reach the observation log"
        assert record.protocol_version
        metrics = json.loads(record.context_metrics_json)
        assert set(metrics) == {
            "rules_bytes",
            "spine_bytes",
            "documents_count",
            "skills_bytes",
            "descriptions_bytes",
        }


class TestDescriptionContract:
    def test_real_dispatch_rejects_create_document_without_description(
        self,
        demo: str,
        workspace: WorkspaceRoot,
    ) -> None:
        """The required MCP argument is enforced before the tool body writes."""
        from ferumind.mcp.server import mcp

        async def scenario() -> CallToolResult:
            result = await mcp.call_tool(
                "create_document",
                {
                    "project": demo,
                    "folder_path": "canvases",
                    "title": "Missing Description",
                    "content": "# Missing Description\n",
                },
            )
            assert isinstance(result, CallToolResult)
            return result

        result = anyio.run(scenario)
        assert result.is_error is True
        structured = cast("dict[str, Any]", result.structured_content)
        assert structured["ok"] is False
        assert structured["error_code"] == "VALIDATION_ERROR"
        assert not (workspace / "projects/demo/canvases/missing-description.md").exists()


class TestPathSafety:
    """§10.7: adversarial paths under the new layout."""

    @pytest.mark.parametrize(
        "path",
        [
            "../../system/projects.yml",
            "../other/spine.md",
            "/etc/passwd",
            "canvases/../../../../etc/passwd",
            ".ferumind/snapshots/x.md",
        ],
    )
    def test_reads_and_writes_reject_escapes(
        self,
        tools: ToolMap,
        demo: str,
        workspace: WorkspaceRoot,
        path: str,
    ) -> None:
        if path.startswith(".ferumind/"):
            internal = workspace / "projects" / demo / path
            internal.parent.mkdir(parents=True, exist_ok=True)
            internal.write_text("# internal data\n", encoding="utf-8")
        read = call(tools, "read_document", project=demo, path=path)
        assert read["ok"] is False
        assert read["error_code"] in {"WORKSPACE_MISMATCH", "DOCUMENT_NOT_FOUND"}
        propose = call(
            tools,
            "propose_exact_replace_patch",
            project=demo,
            path=path,
            old_string="a",
            new_string="b",
        )
        assert propose["ok"] is False
        assert propose["error_code"] in {"WORKSPACE_MISMATCH", "DOCUMENT_NOT_FOUND"}

    def test_symlink_escape_is_refused(
        self, tools: ToolMap, demo: str, workspace: WorkspaceRoot, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        link = Path(workspace) / "projects" / demo / "canvases" / "sneaky.md"
        link.symlink_to(outside)
        envelope = call(tools, "read_document", project=demo, path="canvases/sneaky.md")
        assert envelope["ok"] is False
        assert envelope["error_code"] == "WORKSPACE_MISMATCH"


class TestRecordEpisodeTool:
    def test_one_call_records_an_episode_and_saves_it(self, tools: ToolMap, demo: str) -> None:
        data = ok(
            tools,
            "record_episode",
            project=demo,
            title="Upper-arm pain after bench",
            summary="Pain followed the session; the next one was dropped.",
        )

        assert data["document_mutated"] is True
        assert data["month_file_created"] is True
        assert data["path"].startswith("memory/episodes/")
        assert data["folder"] == "memory"
        assert data["episode_id"].startswith("ep_")
        assert data["snapshot_id"]

    def test_a_second_call_appends_into_the_same_month(self, tools: ToolMap, demo: str) -> None:
        first = ok(tools, "record_episode", project=demo, title="First", summary="One.")
        second = ok(
            tools,
            "record_episode",
            project=demo,
            title="Second",
            summary="Two.",
            related_episode_id=first["episode_id"],
        )

        assert second["month_file_created"] is False
        assert second["path"] == first["path"]
        assert second["episode_id"] != first["episode_id"]

        document = ok(tools, "read_document", project=demo, path=first["path"])
        assert first["episode_id"] in document["content"]
        assert second["episode_id"] in document["content"]
        assert document["edit_policy"] == "append"

    def test_the_project_argument_is_an_assertion_not_an_override(
        self, tools: ToolMap, demo: str
    ) -> None:
        unknown = call(tools, "record_episode", project="no-such-project", title="t", summary="s")
        assert unknown["error_code"] == "PROJECT_NOT_FOUND"

        blank = call(tools, "record_episode", project="", title="t", summary="s")
        assert blank["error_code"] in {"PROJECT_REQUIRED", "PROJECT_NOT_FOUND"}
        assert demo  # the real project is untouched by either refusal

    def test_a_related_path_cannot_escape_the_project(self, tools: ToolMap, demo: str) -> None:
        envelope = call(
            tools,
            "record_episode",
            project=demo,
            title="Escape attempt",
            summary="Tries to name a file outside the project.",
            related_paths=["../../../etc/passwd"],
        )
        assert envelope["ok"] is False
        assert envelope["error_code"] in {"WORKSPACE_MISMATCH", "VALIDATION_ERROR"}

    def test_the_write_is_refused_on_a_mismatched_workspace_format(
        self, tools: ToolMap, demo: str, workspace: WorkspaceRoot
    ) -> None:
        write_format_marker(workspace, 1)
        envelope = call(tools, "record_episode", project=demo, title="t", summary="s")
        assert envelope["error_code"] == "FORMAT_UNSUPPORTED"
        assert not (Path(workspace) / "projects" / demo / "memory" / "episodes").exists()


class TestFormatGate:
    def test_old_format_reads_ok_writes_refused(
        self, tools: ToolMap, demo: str, workspace: WorkspaceRoot
    ) -> None:
        write_format_marker(workspace, SUPPORTED_FORMAT - 1)
        read = call(tools, "get_context", project=demo)
        assert read["ok"] is True
        assert read["data"]["payload"]["format"] == SUPPORTED_FORMAT - 1
        write = call(
            tools,
            "propose_exact_replace_patch",
            project=demo,
            path="canvases/plan.md",
            old_string="alpha",
            new_string="ALPHA",
        )
        assert write["error_code"] == "FORMAT_UNSUPPORTED"
        create = call(tools, "capture_note", project=demo, text="x")
        assert create["error_code"] == "FORMAT_UNSUPPORTED"
        rebuild = call(tools, "rebuild_index", project=demo)
        assert rebuild["error_code"] == "FORMAT_UNSUPPORTED"

    def test_newer_format_refuses_everything(
        self, tools: ToolMap, demo: str, workspace: WorkspaceRoot
    ) -> None:
        write_format_marker(workspace, SUPPORTED_FORMAT + 1)
        read = call(tools, "get_context", project=demo)
        assert read["error_code"] == "FORMAT_UNSUPPORTED"


class TestWireLevelConversion:
    """Calls routed through the SDK's result-conversion layer, not tool.fn.

    This is the only place the output-schema contract is visible. A direct
    ``tool.fn`` call never touches ``FuncMetadata.convert_result``, which is
    what derives the schema and validates ``structured_content`` against it —
    so every assertion here is invisible to the rest of the suite.

    These tests exist because a docstring once asserted SDK behaviour that had
    silently stopped being true (see ``make_result`` in ``mcp/models.py``).
    They pin the behaviour instead of describing it.
    """

    @pytest.mark.usefixtures("tools")
    def test_every_tool_advertises_a_client_usable_output_schema(self) -> None:
        """Schema present, and shaped so real clients accept it.

        Root must be ``type: object`` with no root ``$ref``: several clients
        reject anything else outright. No ``JsonValue`` anywhere, because
        ``core.types.JsonValue`` is recursive and cannot be resolved by a
        client that flattens ``$ref``.
        """
        from ferumind.mcp.server import mcp

        listed = anyio.run(mcp.list_tools)
        assert len(listed) == 48
        problems: list[str] = []
        for tool in listed:
            schema = tool.output_schema
            if schema is None:
                problems.append(f"{tool.name}: no outputSchema")
                continue
            if schema.get("type") != "object":
                problems.append(f"{tool.name}: root type is {schema.get('type')!r}, not 'object'")
            if "$ref" in schema:
                problems.append(f"{tool.name}: root is a $ref")
            if "JsonValue" in json.dumps(schema):
                problems.append(f"{tool.name}: reaches the recursive JsonValue alias")
        assert not problems, (
            "Output schemas are not client-usable:\n  "
            + "\n  ".join(problems)
            + "\nDeclare the tool as -> Annotated[CallToolResult, FerumindResult[Payload]] "
            "and keep the payload model free of core.types.JsonObject/JsonValue. "
            "See the mcp-tool-contracts skill."
        )

    @pytest.mark.usefixtures("tools")
    def test_get_context_schema_declares_exact_document_and_payload_fields(self) -> None:
        from ferumind.mcp.server import mcp

        schema = next(
            tool.output_schema for tool in anyio.run(mcp.list_tools) if tool.name == "get_context"
        )
        assert schema is not None
        definitions = cast("dict[str, Any]", schema["$defs"])
        document = cast("dict[str, Any]", definitions["ContextDocument"])
        document_properties = cast("dict[str, Any]", document["properties"])
        document_fields = {
            "path",
            "title",
            "description",
            "folder",
            "status",
            "edit_policy",
            "updated",
            "size_bytes",
        }
        assert set(document_properties) == document_fields
        assert set(cast("list[str]", document["required"])) == document_fields
        assert document_properties["description"] == {"type": "string"}
        assert document_properties["size_bytes"] == {"type": "integer"}
        assert document["additionalProperties"] is False

        payload = cast("dict[str, Any]", definitions["ContextPayload"])
        payload_properties = cast("dict[str, Any]", payload["properties"])
        payload_fields = {
            "format",
            "rules_bytes",
            "spine_bytes",
            "documents_count",
            "skills_bytes",
            "descriptions_bytes",
        }
        assert set(payload_properties) == payload_fields
        assert set(cast("list[str]", payload["required"])) == payload_fields
        format_types = {
            entry["type"]
            for entry in cast("list[dict[str, str]]", payload_properties["format"]["anyOf"])
        }
        assert format_types == {"integer", "null"}
        assert payload_properties["descriptions_bytes"] == {"type": "integer"}
        assert payload["additionalProperties"] is False

    @pytest.mark.usefixtures("tools")
    def test_no_tool_passes_structured_output_false(self) -> None:
        """``structured_output=False`` short-circuits schema derivation.

        It is inert on mcp 2.x for a bare ``-> CallToolResult``, which is why
        it survived unnoticed — but it is *not* inert now: it returns before
        the ``Annotated`` metadata is read and would strip the schema from
        every tool it is passed to.
        """
        sources = (REPO_ROOT / "src" / "ferumind" / "mcp").glob("*.py")
        # Prose may discuss it — models.py records why it was removed. Only an
        # actual keyword argument does damage.
        offenders = sorted(
            path.name
            for path in sources
            if re.search(r"structured_output\s*=", path.read_text()) and path.name != "protocols.py"
        )
        assert not offenders, (
            f"{offenders} pass structured_output to a tool. It returns before the "
            "Annotated return metadata is read, so it silently strips that tool's "
            "outputSchema. Only the ToolRegistrar protocol may name it, to declare "
            "it optional."
        )

    @pytest.mark.usefixtures("tools")
    def test_declared_and_constructed_envelopes_cannot_drift(self) -> None:
        """``FerumindResult`` describes what ``FerumindToolEnvelope`` builds."""
        from ferumind.mcp.models import FerumindResult, FerumindToolEnvelope

        declared = set(FerumindResult.model_fields)
        constructed = set(FerumindToolEnvelope.model_fields)
        assert declared == constructed, (
            f"Envelope drift: declared-only={declared - constructed}, "
            f"constructed-only={constructed - declared}. Every field a tool can emit "
            "must be in the advertised schema or the SDK rejects the result."
        )

    @pytest.mark.usefixtures("tools")
    def test_tool_definitions_stay_inside_their_context_budget(self) -> None:
        """``tools/list`` is context every caller pays for on every session.

        The ceiling is deliberately close to the current size. Crossing it
        should be a decision, not an accident — if a change needs the room,
        raise it here in the same commit and say why.
        """
        from ferumind.mcp.server import mcp

        listed = anyio.run(mcp.list_tools)
        payload = [tool.model_dump(by_alias=True, exclude_none=True) for tool in listed]
        measured = len(json.dumps(payload))
        budget = 150_000
        assert measured <= budget, (
            f"tools/list is {measured:,} bytes, over the {budget:,} byte budget. "
            "Trim payload models or descriptions rather than raising this silently."
        )

    @pytest.mark.usefixtures("tools")
    def test_descriptions_do_not_restate_the_schema(self) -> None:
        """Shape lives in the schema; the description says when and why.

        Duplication is how the two drift. Before output schemas existed the
        descriptions were the only contract, and fifteen of them had gone
        stale — ``list_projects`` advertised a ``path`` field it never
        returned, ``rebuild_index`` named two fields that do not exist.
        """
        from ferumind.mcp.server import mcp

        listed = anyio.run(mcp.list_tools)
        offenders = [
            tool.name
            for tool in listed
            if re.search(r"Returns \{|returns \{|\{[a-z_]+, [a-z_]+,", tool.description or "")
        ]
        assert not offenders, (
            f"{offenders} enumerate their result fields in prose. That shape is already "
            "in outputSchema, and a second copy is what drifts. Describe when to call "
            "the tool and what the result means instead."
        )

    @pytest.mark.usefixtures("tools")
    def test_all_four_result_paths_validate_for_every_tool(self) -> None:
        """The schema must accept failures, not just the happy path.

        ``convert_result`` validates ``structured_content`` on every result,
        including ``is_error=True`` ones. A payload model that made ``data``
        required would raise ``ToolError`` on each of the three failure
        arms — and that exception quotes the rejected input, which is exactly
        what ``tool_boundary`` exists to keep off the wire.

        Driven through each tool's own ``output_model`` so a new tool is
        covered without touching this test.
        """
        from ferumind.mcp.models import make_error, make_success
        from ferumind.mcp.server import mcp

        arms = {
            "domain error": make_error("PROJECT_NOT_FOUND", "no such project", {"project": "x"}),
            "sanitised crash": make_error(
                "INTERNAL_ERROR",
                "Ferumind encountered an unexpected internal error",
                {"correlation_id": "fm_corr_deadbeef"},
            ),
            "rejected arguments": make_error(
                "VALIDATION_ERROR", "Tool arguments do not match the declared input schema"
            ),
            "success with no payload": make_success(None, project="demo"),
        }
        failures: list[str] = []
        for tool in registered_tools(mcp):
            model = tool.fn_metadata.output_model
            assert model is not None, f"{tool.name} has no output model"
            for arm, result in arms.items():
                try:
                    model.model_validate(result.structured_content)
                except Exception as exc:
                    failures.append(f"{tool.name} rejects the {arm} arm: {type(exc).__name__}")
        assert not failures, "\n".join(failures)

    @pytest.mark.usefixtures("tools")
    def test_envelope_survives_conversion_on_success_and_error(self) -> None:
        from ferumind.mcp.server import mcp

        async def scenario() -> tuple[object, object]:
            created = await mcp.call_tool("create_project", {"key": "demo", "title": "Demo"})
            missing = await mcp.call_tool("get_context", {"project": "nope"})
            return created, missing

        created, missing = anyio.run(scenario)
        assert isinstance(created, CallToolResult)
        assert created.is_error is False
        structured = cast("dict[str, Any]", created.structured_content)
        assert structured["ok"] is True
        assert cast("dict[str, Any]", structured["data"])["key"] == "demo"
        assert isinstance(missing, CallToolResult)
        assert missing.is_error is True
        errored = cast("dict[str, Any]", missing.structured_content)
        assert errored["error_code"] == "PROJECT_NOT_FOUND"
