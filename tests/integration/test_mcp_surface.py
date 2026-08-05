"""Integration tests for the MCP surface v2 — spec-mcp §10 acceptance criteria.

Every tool is called cold through its registered FastMCP function with a
fresh per-test workspace: no tool requires any prior call except
``apply_patch`` (which requires a ``propose_*``).
"""

from __future__ import annotations

import base64
import inspect
import json
import subprocess
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from mcp.types import CallToolResult

from ferumind.core.edit_targets import ExactEdit, InsertAnchor
from ferumind.core.format import write_format_marker
from ferumind.core.observations import list_observations
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.writes import ChatGPTFileInput

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_observed = False

type ToolMap = dict[str, Callable[..., CallToolResult]]


async def _await_tool_result(awaitable: Awaitable[CallToolResult]) -> CallToolResult:
    return await awaitable


@pytest.fixture
def run_test_remote_uploads_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep surface tests focused on results; offload behavior has a dedicated test."""
    from ferumind.mcp import write_tools

    async def run_inline[T](
        func: Callable[..., T],
        *args: object,
        **_kwargs: object,
    ) -> T:
        return func(*args)

    monkeypatch.setattr(write_tools, "run_sync", run_inline)


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


def _ensure_observed() -> None:
    """Apply the observation wrapper exactly once per test session."""
    global _observed
    if _observed:
        return
    from ferumind.mcp.observation import apply_observation_to_all_tools
    from ferumind.mcp.server import mcp

    apply_observation_to_all_tools(mcp)
    _observed = True


@pytest.fixture
def tools(
    workspace: WorkspaceRoot,
    run_test_remote_uploads_inline: None,
) -> Iterator[ToolMap]:
    """The full registered tool surface bound to a fresh workspace."""
    from ferumind.mcp import server, tool_context

    tool_context.reset_tool_context()
    tool_context.init_tool_context(Path(workspace))
    server.register_all_tools()
    _ensure_observed()
    tool_manager = server.mcp._tool_manager  # pyright: ignore[reportPrivateUsage]
    registered = cast("dict[str, Any]", tool_manager._tools)  # pyright: ignore[reportPrivateUsage]
    yield {name: tool.fn for name, tool in registered.items()}
    tool_context.reset_tool_context()


def call(tools: ToolMap, name: str, **kwargs: object) -> dict[str, Any]:
    result = tools[name](**kwargs)
    if inspect.isawaitable(result):
        result = anyio.run(_await_tool_result, cast("Awaitable[CallToolResult]", result))
    assert isinstance(result, CallToolResult)
    structured = result.structuredContent
    assert isinstance(structured, dict)
    return structured


def ok(tools: ToolMap, name: str, **kwargs: object) -> dict[str, Any]:
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
        content="# Plan\n\nalpha beta gamma\n",
    )
    return "demo"


class TestSurfaceShape:
    def test_tool_inventory_includes_workspace_compacts(self, tools: ToolMap) -> None:
        expected = {
            # 17 read
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
            # 17 mutate
            "apply_patch",
            "create_document",
            "upload_library_file",
            "finalize_library_file_upload",
            "upload_library_files_from_chatgpt",
            "upload_library_file_from_chatgpt",
            "capture_note",
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
        assert len(tools) == 46

    @pytest.mark.usefixtures("tools")
    def test_annotation_taxonomy(self) -> None:
        from ferumind.mcp.server import mcp

        registered = cast(
            "dict[str, Any]",
            mcp._tool_manager._tools,  # pyright: ignore[reportPrivateUsage]
        )
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
            assert bool(annotations.openWorldHint) is (name in remote_download), name
            if name in read_only:
                assert annotations.readOnlyHint, name
                assert annotations.idempotentHint, name
            elif name in proposal:
                assert annotations.readOnlyHint, name
                assert not annotations.idempotentHint, name
            else:
                assert not annotations.readOnlyHint, name
                assert not annotations.idempotentHint, name


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

        registered = cast(
            "dict[str, Any]",
            mcp._tool_manager._tools,  # pyright: ignore[reportPrivateUsage]
        )
        for name, tool in registered.items():
            schema = cast("dict[str, Any]", tool.parameters)
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
        from ferumind.core import writes

        monkeypatch.setattr(writes, "fetch_remote_file", _fake_fetch(b"cold call chatgpt bytes"))

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
        from ferumind.core import writes

        monkeypatch.setattr(writes, "MAX_CHUNK_BYTES", 16)
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

        registered = cast(
            "dict[str, Any]",
            mcp._tool_manager._tools,  # pyright: ignore[reportPrivateUsage]
        )
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
        from ferumind.core import writes

        monkeypatch.setattr(writes, "fetch_remote_file", _fake_fetch(b"downloaded chatgpt bytes"))
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
        from ferumind.core import writes

        monkeypatch.setattr(writes, "fetch_remote_file", _fake_fetch_echoing_url())
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
        from ferumind.core import writes
        from ferumind.core.errors import DownloadFailedError

        def fake_fetch(url: str, **kwargs: object) -> bytes:
            if "bad" in url:
                raise DownloadFailedError("simulated failure")
            return b"ok bytes"

        monkeypatch.setattr(writes, "fetch_remote_file", fake_fetch)
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

        registered = cast(
            "dict[str, Any]",
            mcp._tool_manager._tools,  # pyright: ignore[reportPrivateUsage]
        )
        properties = registered["upload_library_files_from_chatgpt"].parameters["properties"]
        assert set(properties) == {"project", "files", "folder_path"}


class TestChatGPTSingleFileUpload:
    """upload_library_file_from_chatgpt: one file, one caller-chosen filename."""

    def test_tool_descriptor_declares_a_single_top_level_file_param(self) -> None:
        from ferumind.mcp.server import mcp

        registered = cast(
            "dict[str, Any]",
            mcp._tool_manager._tools,  # pyright: ignore[reportPrivateUsage]
        )
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
        from ferumind.core import writes

        monkeypatch.setattr(writes, "fetch_remote_file", _fake_fetch(b"single file bytes"))
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
        from ferumind.core import writes
        from ferumind.core.errors import DownloadFailedError

        def fake_fetch(url: str, **kwargs: object) -> bytes:
            raise DownloadFailedError("simulated failure")

        monkeypatch.setattr(writes, "fetch_remote_file", fake_fetch)
        result = call(
            tools,
            "upload_library_file_from_chatgpt",
            project=demo,
            file=ChatGPTFileInput(download_url="https://chatgpt.example/x", file_id="f1"),
            filename="nope.bin",
        )
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
        assert payload["format"] == 2
        assert payload["rules_bytes"] > 0
        assert payload["spine_bytes"] > 0
        assert payload["documents_count"] == len(data["documents"])
        assert data["project"] == {"key": "demo", "title": "Demo", "status": "active"}
        assert data["rules"]["sources"][0] == "system/rules/00-contract.md"
        assert data["spine"]["path"] == "spine.md"

    def test_observation_rows_carry_payload_metrics(self, tools: ToolMap, demo: str) -> None:
        """§10.6: get_context observations carry the three payload metrics."""
        from ferumind.mcp.tool_context import require_database

        ok(tools, "get_context", project=demo)
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
        metrics = json.loads(record.context_metrics_json)
        assert set(metrics) == {"rules_bytes", "spine_bytes", "documents_count"}


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


class TestFormatGate:
    def test_old_format_reads_ok_writes_refused(
        self, tools: ToolMap, demo: str, workspace: WorkspaceRoot
    ) -> None:
        write_format_marker(workspace, 1)
        read = call(tools, "get_context", project=demo)
        assert read["ok"] is True
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
        write_format_marker(workspace, 3)
        read = call(tools, "get_context", project=demo)
        assert read["error_code"] == "FORMAT_UNSUPPORTED"


class TestWireLevelConversion:
    """Calls routed through FastMCP's result-conversion layer, not tool.fn.

    Regression guard: the ``-> CallToolResult`` return annotations once made
    FastMCP generate a bogus outputSchema and its convert_result step raised
    on every real client call, while direct ``tool.fn`` tests stayed green.
    Tools register with ``structured_output=False`` so the envelope passes
    through the SDK verbatim.
    """

    @pytest.mark.usefixtures("tools")
    def test_no_tool_advertises_an_output_schema(self) -> None:
        from ferumind.mcp.server import mcp

        listed = anyio.run(mcp.list_tools)
        assert len(listed) == 46
        assert all(tool.outputSchema is None for tool in listed)

    @pytest.mark.usefixtures("tools")
    def test_envelope_survives_conversion_on_success_and_error(self) -> None:
        from ferumind.mcp.server import mcp

        async def scenario() -> tuple[object, object]:
            created = await mcp.call_tool("create_project", {"key": "demo", "title": "Demo"})
            missing = await mcp.call_tool("get_context", {"project": "nope"})
            return created, missing

        created, missing = anyio.run(scenario)
        assert isinstance(created, CallToolResult)
        assert created.isError is False
        structured = cast("dict[str, Any]", created.structuredContent)
        assert structured["ok"] is True
        assert cast("dict[str, Any]", structured["data"])["key"] == "demo"
        assert isinstance(missing, CallToolResult)
        assert missing.isError is True
        errored = cast("dict[str, Any]", missing.structuredContent)
        assert errored["error_code"] == "PROJECT_NOT_FOUND"
