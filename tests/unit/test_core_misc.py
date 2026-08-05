"""Tests for config loading, policy echo, and the MCP envelope."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from ferumind.core.config import load_config
from ferumind.core.documents import parse_document_content
from ferumind.core.errors import ERROR_CODES, FerumindError, PatchConflictError
from ferumind.core.file_io import atomic_write_text
from ferumind.core.frontmatter import generate_frontmatter
from ferumind.core.policy import FROZEN_NOTE, POLICY_NOTES, policy_echo_for
from ferumind.mcp.models import (
    apply_state_fields,
    make_error,
    make_success,
    proposal_annotations,
    proposal_state_fields,
    read_only_annotations,
    write_annotations,
)


class TestConfig:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in (
            "FERUMIND_WORKSPACE",
            "FERUMIND_LOG_LEVEL",
        ):
            monkeypatch.delenv(key, raising=False)
        config = load_config()
        assert config.workspace_path == Path("./workspace")

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FERUMIND_WORKSPACE", "/data/ws")
        config = load_config()
        assert config.workspace_path == Path("/data/ws")

    def test_explicit_workspace_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FERUMIND_WORKSPACE", "/data/ws")
        config = load_config(Path("/explicit"))
        assert config.workspace_path == Path("/explicit")

    def test_invalid_log_level_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FERUMIND_LOG_LEVEL", "VERBOSE")
        with pytest.raises(PydanticValidationError):
            load_config()


def test_atomic_write_creates_private_parent_and_file(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "value.txt"
    atomic_write_text(target, "private\n")
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert target.stat().st_mode & 0o777 == 0o600


class TestPolicyEcho:
    def _doc(self, *, status: str = "active", edit_policy: str | None = None):
        fm = generate_frontmatter(
            doc_id="doc_x", project_key="demo", title="T", status=status, edit_policy=edit_policy
        )
        return parse_document_content(fm + "body\n", project_key="demo", path="canvases/x.md")

    def test_free_has_no_note(self) -> None:
        echo = policy_echo_for(self._doc())
        assert echo.edit_policy == "free"
        assert echo.policy_note is None

    @pytest.mark.parametrize("policy", ["append", "propose-first", "ask-human"])
    def test_notes_match_spec_strings(self, policy: str) -> None:
        echo = policy_echo_for(self._doc(edit_policy=policy))
        assert echo.policy_note == POLICY_NOTES[policy]

    def test_frozen_status_overrides_policy_note(self) -> None:
        echo = policy_echo_for(self._doc(status="frozen", edit_policy="append"))
        assert echo.policy_note == FROZEN_NOTE
        assert echo.status == "frozen"


class TestEnvelope:
    def test_make_success_shapes_content_and_structured(self) -> None:
        result = make_success({"x": 1}, project="demo")
        assert result.isError is False
        assert result.structuredContent == {"ok": True, "data": {"x": 1}, "project": "demo"}
        text = result.content[0]
        payload = json.loads(getattr(text, "text"))  # noqa: B009
        assert payload["ok"] is True

    def test_make_error_carries_code_and_details(self) -> None:
        result = make_error("PATCH_CONFLICT", "boom", {"reason": "out-of-band-edit"})
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error_code"] == "PATCH_CONFLICT"
        assert result.structuredContent["details"] == {"reason": "out-of-band-edit"}

    def test_state_fields(self) -> None:
        pending = proposal_state_fields("op_1", project="demo")
        assert pending["document_mutated"] is False
        assert pending["requires_apply"] is True
        assert pending["next_required_tool"] == "apply_patch"
        assert pending["next_required_arguments"] == {"operation_id": "op_1", "project": "demo"}
        saved = apply_state_fields("op_1", "op_2")
        assert saved["document_mutated"] is True
        assert saved["proposal_operation_id"] == "op_1"

    def test_annotation_taxonomy(self) -> None:
        read = read_only_annotations()
        assert read.readOnlyHint is True
        assert read.idempotentHint is True
        proposal = proposal_annotations()
        assert proposal.readOnlyHint is True
        assert proposal.idempotentHint is False
        write = write_annotations()
        assert write.readOnlyHint is False
        assert write.idempotentHint is False


class TestErrors:
    def test_error_codes_are_stable_and_session_free(self) -> None:
        assert "FORMAT_UNSUPPORTED" in ERROR_CODES
        assert "PATCH_EXPIRED" in ERROR_CODES
        assert not any("SESSION" in code for code in ERROR_CODES)

    def test_errors_carry_code_and_details(self) -> None:
        exc = PatchConflictError("boom", details={"a": 1})
        assert exc.code == "PATCH_CONFLICT"
        assert exc.details == {"a": 1}
        assert isinstance(exc, FerumindError)
        assert isinstance(exc, ValueError)
