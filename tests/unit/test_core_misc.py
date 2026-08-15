"""Tests for config loading, policy echo, and the MCP envelope."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from ferumind.core.config import load_config
from ferumind.core.documents import parse_document_content
from ferumind.core.errors import ERROR_CODES, FerumindError, PatchConflictError
from ferumind.core.file_io import (
    atomic_write_text,
    ensure_private_directory,
    read_regular_file_bytes,
)
from ferumind.core.frontmatter import FrontmatterBehavior, generate_frontmatter
from ferumind.core.paths import PathSafetyError
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
from tests.conftest import TEST_DESCRIPTION


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

    def test_resource_ceiling_defaults_to_the_tunnel_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("FERUMIND_MAX_RESOURCE_MB", raising=False)
        assert load_config().max_resource_response_bytes == 10 * 1024 * 1024

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("10", 10 * 1024 * 1024),
            ("4", 4 * 1024 * 1024),
            ("0.5", 512 * 1024),  # fractions keep the 64 KiB floor reachable
            (" 2 ", 2 * 1024 * 1024),
        ],
    )
    def test_resource_ceiling_converts_megabytes_to_bytes(
        self, monkeypatch: pytest.MonkeyPatch, value: str, expected: int
    ) -> None:
        monkeypatch.setenv("FERUMIND_MAX_RESOURCE_MB", value)
        assert load_config().max_resource_response_bytes == expected

    @pytest.mark.parametrize("value", ["", "ten", "0", "-1", "nan", "inf"])
    def test_resource_ceiling_rejects_unusable_values(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """An empty value means unset; everything else here must fail loudly.

        ``0`` in particular must not fall through to the default: a caller
        writing it means to forbid something, and silently serving 10 MiB
        instead is the opposite of that intent.
        """
        monkeypatch.setenv("FERUMIND_MAX_RESOURCE_MB", value)
        if value == "":
            assert load_config().max_resource_response_bytes == 10 * 1024 * 1024
            return
        with pytest.raises(ValueError, match="FERUMIND_MAX_RESOURCE_MB"):
            load_config()


def test_atomic_write_creates_private_parent_and_file(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "value.txt"
    atomic_write_text(target, "private\n")
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert target.stat().st_mode & 0o777 == 0o600


def test_ensure_private_directory_creates_every_missing_parent_privately(tmp_path: Path) -> None:
    """``mkdir(parents=True)`` applies its mode to the leaf only.

    A private directory reached through a world-listable parent is not
    private, and nothing about the leaf's own mode reveals that.
    """
    leaf = tmp_path / "one" / "two" / "three"

    ensure_private_directory(leaf)

    for created in (leaf, leaf.parent, leaf.parent.parent):
        assert created.stat().st_mode & 0o777 == 0o700, f"{created} is not private"


def test_ensure_private_directory_keeps_an_existing_directory_as_the_operator_set_it(
    tmp_path: Path,
) -> None:
    """S-09. An operator who widens a workspace directory keeps that choice."""
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o700)
    shared.chmod(0o750)

    ensure_private_directory(shared)

    assert shared.stat().st_mode & 0o777 == 0o750


def test_atomic_write_does_not_revert_an_operators_directory_mode(tmp_path: Path) -> None:
    """S-09. The reverting chmod ran on every write, not only on create."""
    target = tmp_path / "documents" / "note.md"
    atomic_write_text(target, "first\n")
    target.parent.chmod(0o750)

    atomic_write_text(target, "second\n")

    assert target.parent.stat().st_mode & 0o777 == 0o750
    assert target.read_text(encoding="utf-8") == "second\n"


def test_atomic_write_preserves_an_existing_files_mode(tmp_path: Path) -> None:
    """A rename carries the temporary file's mode onto the destination.

    Left alone that resets an operator-chosen mode to mkstemp's 0600 on every
    save — the same defect as the directory chmod, one level down, and the
    half S-09 did not name.
    """
    target = tmp_path / "note.md"
    atomic_write_text(target, "first\n")
    target.chmod(0o640)

    atomic_write_text(target, "second\n")

    assert target.stat().st_mode & 0o777 == 0o640
    assert target.read_text(encoding="utf-8") == "second\n"


def test_atomic_write_never_widens_a_new_file(tmp_path: Path) -> None:
    """Preserving an existing mode must not become inventing a permissive one."""
    target = tmp_path / "fresh.md"

    atomic_write_text(target, "new\n")

    assert target.stat().st_mode & 0o777 == 0o600


# ── S-06: validation and use must name the same object ──────────────────────
#
# `contained_path` refuses symlinks when it validates, then returns a Path
# somebody opens later. These tests stand in for an attacker winning that
# window: the symlink is planted *after* validation would have run, which is
# the only arrangement the old code could not see.


def test_reading_refuses_a_symlink_swapped_in_after_validation(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    target = tmp_path / "inside.txt"
    target.symlink_to(outside)

    with pytest.raises(PathSafetyError):
        read_regular_file_bytes(target)


def test_reading_refuses_a_path_that_is_not_a_regular_file(tmp_path: Path) -> None:
    """The check runs on fstat of the opened descriptor, not a stat of the name."""
    directory = tmp_path / "a-directory"
    directory.mkdir()

    with pytest.raises((PathSafetyError, IsADirectoryError)):
        read_regular_file_bytes(directory)


def test_reading_a_plain_file_is_unchanged(tmp_path: Path) -> None:
    target = tmp_path / "plain.txt"
    target.write_bytes(b"payload\n")

    assert read_regular_file_bytes(target) == b"payload\n"


def test_atomic_write_follows_the_directory_it_opened_not_the_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write finishes in the directory it opened, not one swapped in mid-flight.

    The swap is performed *during* the write, at the first ``fsync`` — inside
    the window between resolving the parent and renaming the payload into it,
    which is the only place the race exists. A swap staged before the call is
    not a race and passes against either implementation.

    Path-based code loses this: it resolves the parent by name again at the
    rename and lands the bytes in the attacker's directory. Because every step
    here is relative to the descriptor, the bytes follow the original inode.
    """
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    decoy = tmp_path / "decoy"
    decoy.mkdir(mode=0o700)

    swapped = False
    real_fsync = os.fsync

    def swap_then_fsync(fd: int) -> None:
        nonlocal swapped
        if not swapped:
            swapped = True
            real.rename(tmp_path / "moved")
            decoy.rename(tmp_path / "real")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", swap_then_fsync)

    atomic_write_text(real / "note.md", "payload\n")

    assert swapped, "the race was never triggered; the test proves nothing"
    assert (tmp_path / "moved" / "note.md").read_text(encoding="utf-8") == "payload\n"
    assert not (tmp_path / "real" / "note.md").exists()


def test_atomic_write_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    """O_EXCL plus the unlink path must not litter on success or on failure."""
    target = tmp_path / "note.md"
    atomic_write_text(target, "first\n")
    atomic_write_text(target, "second\n")

    leftovers = [child.name for child in tmp_path.iterdir() if ".ferumind_tmp_" in child.name]
    assert leftovers == []


class TestPolicyEcho:
    def _doc(self, *, status: str = "active", edit_policy: str | None = None):
        fm = generate_frontmatter(
            description=TEST_DESCRIPTION,
            doc_id="doc_x",
            project_key="demo",
            title="T",
            behavior=FrontmatterBehavior(status=status, edit_policy=edit_policy),
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
        assert result.is_error is False
        assert result.structured_content == {"ok": True, "data": {"x": 1}, "project": "demo"}
        text = result.content[0]
        payload = json.loads(getattr(text, "text"))  # noqa: B009
        assert payload["ok"] is True

    def test_make_error_carries_code_and_details(self) -> None:
        result = make_error("PATCH_CONFLICT", "boom", {"reason": "out-of-band-edit"})
        assert result.is_error is True
        assert result.structured_content is not None
        assert result.structured_content["error_code"] == "PATCH_CONFLICT"
        assert result.structured_content["details"] == {"reason": "out-of-band-edit"}

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
        assert read.read_only_hint is True
        assert read.idempotent_hint is True
        proposal = proposal_annotations()
        assert proposal.read_only_hint is True
        assert proposal.idempotent_hint is False
        write = write_annotations()
        assert write.read_only_hint is False
        assert write.idempotent_hint is False


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
