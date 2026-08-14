"""Tests for upload_library_files_from_chatgpt: batch download-and-store from
ChatGPT's openai/fileParams file references, with per-file partial-success semantics.

The actual SSRF-hardened HTTP fetch is exercised in test_remote_fetch.py;
these tests monkeypatch ferumind.core.upload_writes.fetch_remote_file so batch
orchestration (per-file error isolation, filename/mime handling, dedup
non-claim) can be tested without any network layer at all.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import threading
from unittest import mock

import pytest
from pydantic import ValidationError as PydanticValidationError

from ferumind.core import upload_writes
from ferumind.core.errors import (
    DocumentExistsError,
    DownloadFailedError,
    UnknownFolderError,
    UnsupportedFileTypeError,
    ValidationError,
)
from ferumind.core.paths import WorkspaceRoot
from ferumind.core.upload_writes import ChatGPTFileInput, upload_library_files_from_chatgpt
from ferumind.db.database import Database


def _read_bytes(workspace: WorkspaceRoot, project: str, rel: str) -> bytes:
    return (workspace / "projects" / project / rel).read_bytes()


def _fake_fetch_map(mapping: dict[str, bytes | Exception]) -> object:
    def fake_fetch(url: str, **kwargs: object) -> bytes:
        outcome = mapping[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return fake_fetch


class TestChatGPTBatchUpload:
    def test_multiple_files_succeed(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map(
                {
                    "https://chatgpt.example/a": b"content A",
                    "https://chatgpt.example/b": b"content B",
                }
            ),
        )
        result = upload_library_files_from_chatgpt(
            conn,
            workspace,
            project,
            files=[
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/a",
                    file_id="file_a",
                    file_name="alpha.txt",
                    mime_type="text/plain",
                ),
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/b",
                    file_id="file_b",
                    file_name="beta.txt",
                    mime_type="text/plain",
                ),
            ],
        )
        assert result.succeeded == 2
        assert result.failed == 0
        assert [r.ok for r in result.results] == [True, True]
        assert result.results[0].path == "library/alpha.txt"
        assert result.results[1].path == "library/beta.txt"
        assert _read_bytes(workspace, project, "library/alpha.txt") == b"content A"
        assert result.results[0].sha256 == hashlib.sha256(b"content A").hexdigest()

        sidecar = json.loads(_read_bytes(workspace, project, "library/alpha.json"))
        assert sidecar["chatgpt_file_id"] == "file_a"

    def test_partial_failure_does_not_lose_other_results(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map(
                {
                    "https://chatgpt.example/ok1": b"good file one",
                    "https://chatgpt.example/bad": DownloadFailedError("simulated network failure"),
                    "https://chatgpt.example/ok2": b"good file two",
                }
            ),
        )
        result = upload_library_files_from_chatgpt(
            conn,
            workspace,
            project,
            files=[
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/ok1", file_id="f1", file_name="one.txt"
                ),
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/bad", file_id="f2", file_name="two.txt"
                ),
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/ok2", file_id="f3", file_name="three.txt"
                ),
            ],
        )
        assert result.succeeded == 2
        assert result.failed == 1
        assert len(result.results) == 3  # every file gets a result, none disappear

        by_id = {r.file_id: r for r in result.results}
        assert by_id["f1"].ok is True
        assert by_id["f1"].path == "library/one.txt"
        assert by_id["f2"].ok is False
        assert by_id["f2"].error_code == "DOWNLOAD_FAILED"
        assert "simulated network failure" in (by_id["f2"].error_message or "")
        assert by_id["f2"].path is None
        assert by_id["f3"].ok is True
        assert by_id["f3"].path == "library/three.txt"

    def test_batch_enforces_an_aggregate_download_byte_limit(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(upload_writes, "MAX_CHATGPT_BATCH_BYTES", 5)
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map(
                {
                    "https://chatgpt.example/a": b"1234",
                    "https://chatgpt.example/b": b"5678",
                }
            ),
        )

        result = upload_library_files_from_chatgpt(
            conn,
            workspace,
            project,
            files=[
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/a",
                    file_id="a",
                    file_name="a.bin",
                ),
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/b",
                    file_id="b",
                    file_name="b.bin",
                ),
            ],
        )

        assert result.succeeded == 1
        assert result.failed == 1
        assert result.results[1].error_code == "FILE_TOO_LARGE"
        assert not (workspace / "projects" / project / "library/b.bin").exists()

    def test_batch_enforces_an_aggregate_wall_clock_limit_before_fetch(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = iter([100.0, 161.0])
        monkeypatch.setattr("ferumind.core.upload_writes.time.monotonic", lambda: next(clock))

        def never_fetch(_url: str, **_kwargs: object) -> bytes:
            pytest.fail("expired aggregate budget must be checked before fetching")

        monkeypatch.setattr(upload_writes, "fetch_remote_file", never_fetch)
        result = upload_library_files_from_chatgpt(
            conn,
            workspace,
            project,
            files=[
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/a",
                    file_id="a",
                    file_name="a.bin",
                )
            ],
        )

        assert result.succeeded == 0
        assert result.results[0].error_code == "DOWNLOAD_TIMEOUT"

    def test_filename_falls_back_to_file_id_and_guessed_extension(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map({"https://chatgpt.example/x": b"\xff\xd8\xff"}),
        )
        result = upload_library_files_from_chatgpt(
            conn,
            workspace,
            project,
            files=[
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/x",
                    file_id="file_abc123",
                    mime_type="image/jpeg",
                )
            ],
        )
        assert result.succeeded == 1
        assert result.results[0].path == "library/file_abc123.jpg"

    def test_mime_type_is_normalized(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map({"https://chatgpt.example/x": b"data"}),
        )
        result = upload_library_files_from_chatgpt(
            conn,
            workspace,
            project,
            files=[
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/x",
                    file_id="f1",
                    file_name="a.bin",
                    mime_type="  IMAGE/JPEG; charset=binary  ",
                )
            ],
        )
        assert result.results[0].mime_type == "image/jpeg"
        sidecar = json.loads(_read_bytes(workspace, project, "library/a.json"))
        assert sidecar["mime_type"] == "image/jpeg"

    def test_invalid_mime_is_rejected_per_file_before_any_fetch(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fetched: list[str] = []

        def fake_fetch(url: str, **_kwargs: object) -> bytes:
            fetched.append(url)
            return b"good"

        monkeypatch.setattr(upload_writes, "fetch_remote_file", fake_fetch)
        result = upload_library_files_from_chatgpt(
            conn,
            workspace,
            project,
            files=[
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/bad",
                    file_id="bad",
                    file_name="bad.bin",
                    mime_type="application/octet-stream\nInjected: value",
                ),
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/good",
                    file_id="good",
                    file_name="good.bin",
                    mime_type="application/octet-stream",
                ),
            ],
        )

        assert result.results[0].error_code == "VALIDATION_ERROR"
        assert result.results[1].ok
        assert fetched == ["https://chatgpt.example/good"]

    @pytest.mark.parametrize("filename", ["payload.sh", "notes.md"])
    def test_blocked_extension_is_a_per_file_failure(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
        filename: str,
    ) -> None:
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map({"https://chatgpt.example/x": b"#!/bin/sh"}),
        )
        result = upload_library_files_from_chatgpt(
            conn,
            workspace,
            project,
            files=[
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/x", file_id="f1", file_name=filename
                )
            ],
        )
        assert result.succeeded == 0
        assert result.results[0].error_code == "UNSUPPORTED_FILE_TYPE"

    def test_collision_is_a_per_file_failure(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        upload_writes.upload_library_file(
            conn, workspace, project, filename="taken.txt", content_base64="dGFrZW4="
        )
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map({"https://chatgpt.example/x": b"new"}),
        )
        result = upload_library_files_from_chatgpt(
            conn,
            workspace,
            project,
            files=[
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/x", file_id="f1", file_name="taken.txt"
                )
            ],
        )
        assert result.results[0].error_code == "DOCUMENT_EXISTS"

    def test_no_automatic_dedup_by_file_id(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Idempotency is explicitly NOT claimed — resending the same file_id is not deduped."""
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map(
                {
                    "https://chatgpt.example/x": b"version one",
                    "https://chatgpt.example/y": b"version two",
                }
            ),
        )
        result = upload_library_files_from_chatgpt(
            conn,
            workspace,
            project,
            files=[
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/x", file_id="same_id", file_name="v1.txt"
                ),
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/y", file_id="same_id", file_name="v2.txt"
                ),
            ],
        )
        assert result.succeeded == 2
        assert {r.path for r in result.results} == {"library/v1.txt", "library/v2.txt"}

    def test_empty_files_list_rejected(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(ValidationError):
            upload_library_files_from_chatgpt(conn, workspace, project, files=[])

    def test_too_many_files_rejected(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(upload_writes, "MAX_CHATGPT_FILES_PER_CALL", 2)
        with pytest.raises(ValidationError):
            upload_library_files_from_chatgpt(
                conn,
                workspace,
                project,
                files=[
                    ChatGPTFileInput(download_url=f"https://x/{i}", file_id=f"f{i}")
                    for i in range(3)
                ],
            )

    def test_folder_path_outside_library_rejects_whole_batch(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(UnknownFolderError):
            upload_library_files_from_chatgpt(
                conn,
                workspace,
                project,
                files=[ChatGPTFileInput(download_url="https://x/1", file_id="f1")],
                folder_path="canvases",
            )

    def test_bad_file_name_is_a_per_file_failure(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map({"https://chatgpt.example/x": b"data"}),
        )
        result = upload_library_files_from_chatgpt(
            conn,
            workspace,
            project,
            files=[
                ChatGPTFileInput(
                    download_url="https://chatgpt.example/x",
                    file_id="f1",
                    file_name="../escape.txt",
                )
            ],
        )
        assert result.results[0].ok is False
        assert result.results[0].error_code == "VALIDATION_ERROR"


class TestChatGPTSingleFileIdentity:
    """upload_library_file_from_chatgpt: the filename↔file relationship is 1:1 by construction.

    These are the identity tests for ChatGPT uploads. The batch tool takes
    no caller-supplied names precisely because ``openai/fileParams`` gives
    the model no stable per-file handle to bind one to (the host fills the
    parameter in after the model upload_writes the call), so the only safe way to
    choose a destination filename is one file per call. Each test below
    perturbs something a positional-mapping implementation would get wrong —
    argument order, completion order, duplicate names, identical content,
    rewritten transport URLs — and asserts the name still lands on the bytes
    it was asked for.
    """

    def test_explicit_filename_wins_over_chatgpt_suggested_name(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(1) The name comes from the caller's argument, never from the resolved reference."""
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map({"https://chatgpt.example/x": b"bytes X"}),
        )
        result = upload_writes.upload_library_file_from_chatgpt(
            conn,
            workspace,
            project,
            file=ChatGPTFileInput(
                download_url="https://chatgpt.example/x",
                file_id="file_x",
                file_name="IMG_9999.jpg",
            ),
            filename="flex-01-front.jpg",
        )
        assert result.path == "library/flex-01-front.jpg"
        assert result.filename == "flex-01-front.jpg"
        assert _read_bytes(workspace, project, "library/flex-01-front.jpg") == b"bytes X"
        assert not (workspace / "projects" / project / "library" / "IMG_9999.jpg").exists()

    def test_out_of_order_download_completion_keeps_names_on_their_own_bytes(
        self, database: Database, workspace: WorkspaceRoot, project: str
    ) -> None:
        """(2) Two concurrent calls; the second download finishes first. Neither name migrates."""
        second_done = threading.Event()
        first_started = threading.Event()

        def fake_fetch(url: str, **kwargs: object) -> bytes:
            if url.endswith("/slow"):
                first_started.set()
                # Do not return until the other call has fully completed, so
                # completion order is the reverse of invocation order.
                assert second_done.wait(timeout=10)
                return b"slow bytes"
            first_started.wait(timeout=10)
            return b"fast bytes"

        results: dict[str, upload_writes.ChatGPTSingleUploadResult | BaseException] = {}

        def upload(label: str, url: str, filename: str) -> None:
            conn = database.get_connection()
            try:
                results[label] = upload_writes.upload_library_file_from_chatgpt(
                    conn,
                    workspace,
                    project,
                    file=ChatGPTFileInput(download_url=url, file_id=f"file_{label}"),
                    filename=filename,
                )
            except BaseException as exc:
                # A thread that raises would otherwise fail silently; the
                # assertions below surface it on the main thread instead.
                results[label] = exc
            finally:
                conn.close()

        with mock.patch.object(upload_writes, "fetch_remote_file", fake_fetch):
            slow = threading.Thread(
                target=upload, args=("slow", "https://chatgpt.example/slow", "first-called.bin")
            )
            fast = threading.Thread(
                target=upload, args=("fast", "https://chatgpt.example/fast", "second-called.bin")
            )
            slow.start()
            fast.start()
            fast.join(timeout=15)
            second_done.set()
            slow.join(timeout=15)

        assert not slow.is_alive()
        assert not fast.is_alive()
        for outcome in results.values():
            assert not isinstance(outcome, BaseException), outcome

        assert _read_bytes(workspace, project, "library/first-called.bin") == b"slow bytes"
        assert _read_bytes(workspace, project, "library/second-called.bin") == b"fast bytes"

    def test_two_files_sharing_an_original_filename_stay_distinct(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(3) Identical ChatGPT-suggested names; the caller's names disambiguate."""
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map(
                {
                    "https://chatgpt.example/a": b"front pose",
                    "https://chatgpt.example/b": b"back pose",
                }
            ),
        )
        for url, file_id, filename in (
            ("https://chatgpt.example/a", "file_a", "relaxed-01-front.jpg"),
            ("https://chatgpt.example/b", "file_b", "relaxed-01-back.jpg"),
        ):
            upload_writes.upload_library_file_from_chatgpt(
                conn,
                workspace,
                project,
                file=ChatGPTFileInput(download_url=url, file_id=file_id, file_name="IMG_0001.jpg"),
                filename=filename,
            )
        assert _read_bytes(workspace, project, "library/relaxed-01-front.jpg") == b"front pose"
        assert _read_bytes(workspace, project, "library/relaxed-01-back.jpg") == b"back pose"

    def test_identical_content_under_different_ids_is_not_collapsed(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(4) Same bytes, different opaque ids — content hash is not an identity."""
        same = b"byte-for-byte identical"
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map({"https://chatgpt.example/1": same, "https://chatgpt.example/2": same}),
        )
        first = upload_writes.upload_library_file_from_chatgpt(
            conn,
            workspace,
            project,
            file=ChatGPTFileInput(download_url="https://chatgpt.example/1", file_id="file_one"),
            filename="copy-one.bin",
        )
        second = upload_writes.upload_library_file_from_chatgpt(
            conn,
            workspace,
            project,
            file=ChatGPTFileInput(download_url="https://chatgpt.example/2", file_id="file_two"),
            filename="copy-two.bin",
        )
        assert first.sha256 == second.sha256
        assert first.file_id == "file_one"
        assert second.file_id == "file_two"
        assert (
            json.loads(_read_bytes(workspace, project, "library/copy-one.json"))["chatgpt_file_id"]
            == "file_one"
        )
        assert (
            json.loads(_read_bytes(workspace, project, "library/copy-two.json"))["chatgpt_file_id"]
            == "file_two"
        )

    def test_download_failure_raises_and_writes_nothing(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(5) With one file there is no partial success to report — it is a plain error."""
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map({"https://chatgpt.example/x": DownloadFailedError("network down")}),
        )
        with pytest.raises(DownloadFailedError):
            upload_writes.upload_library_file_from_chatgpt(
                conn,
                workspace,
                project,
                file=ChatGPTFileInput(download_url="https://chatgpt.example/x", file_id="f1"),
                filename="never-written.bin",
            )
        assert not (workspace / "projects" / project / "library" / "never-written.bin").exists()
        assert not (workspace / "projects" / project / "library" / "never-written.json").exists()

    def test_no_binding_parameter_exists_to_get_wrong(self) -> None:
        """(6, 7, 8) There is no name↔reference binding argument, so it cannot mismatch.

        A binding list would need a stable handle the model can name; the
        host supplies the file reference itself, so no such handle exists.
        The signature keeps ``filename`` a required scalar instead.

        ``image_policy`` is a server-side storage-normalization policy, not a
        name-to-reference binding: it carries no handle the model could name
        and cannot be mismatched against a file. The set is asserted exactly
        so any future binding-shaped parameter still fails this test.
        """
        signature = inspect.signature(upload_writes.upload_library_file_from_chatgpt)
        assert set(signature.parameters) == {
            "conn",
            "workspace_root",
            "project_key",
            "file",
            "filename",
            "folder_path",
            "image_policy",
        }
        assert signature.parameters["filename"].default is inspect.Parameter.empty
        assert signature.parameters["file"].annotation == "ChatGPTFileInput"

    def test_filename_is_unaffected_by_transport_url_rewriting(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(9) The same file_id resolving to a different URL changes nothing about the name.

        ChatGPT is known to re-resolve the same file to a fresh
        ``download_url``; the URL is transport, never identity.
        """
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map(
                {"https://cdn.example/rewritten/opaque-token-9f2b?sig=abc": b"the real bytes"}
            ),
        )
        result = upload_writes.upload_library_file_from_chatgpt(
            conn,
            workspace,
            project,
            file=ChatGPTFileInput(
                download_url="https://cdn.example/rewritten/opaque-token-9f2b?sig=abc",
                file_id="file_stable",
                file_name="opaque-token-9f2b",
            ),
            filename="progress-2026-07-27.jpg",
        )
        assert result.path == "library/progress-2026-07-27.jpg"
        assert (
            _read_bytes(workspace, project, "library/progress-2026-07-27.jpg") == b"the real bytes"
        )

    def test_stable_reference_is_persisted_and_returned(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """(10) file_id survives into both the result and the metadata sidecar."""
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map({"https://chatgpt.example/x": b"traceable"}),
        )
        result = upload_writes.upload_library_file_from_chatgpt(
            conn,
            workspace,
            project,
            file=ChatGPTFileInput(
                download_url="https://chatgpt.example/x",
                file_id="file_traceable_123",
                mime_type="image/jpeg",
            ),
            filename="traced.jpg",
        )
        assert result.file_id == "file_traceable_123"
        assert result.mime_type == "image/jpeg"
        assert result.sha256 == hashlib.sha256(b"traceable").hexdigest()
        sidecar = json.loads(_read_bytes(workspace, project, "library/traced.json"))
        assert sidecar["chatgpt_file_id"] == "file_traceable_123"
        assert sidecar["original_filename"] == "traced.jpg"
        assert sidecar["uploaded_by_tool"] == "upload_library_file_from_chatgpt"


class TestChatGPTSingleFileValidation:
    def test_collision_fails_closed(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            upload_writes,
            "fetch_remote_file",
            _fake_fetch_map({"https://chatgpt.example/x": b"new"}),
        )
        upload_writes.upload_library_file(
            conn, workspace, project, filename="taken.txt", content_base64="dGFrZW4="
        )
        with pytest.raises(DocumentExistsError):
            upload_writes.upload_library_file_from_chatgpt(
                conn,
                workspace,
                project,
                file=ChatGPTFileInput(download_url="https://chatgpt.example/x", file_id="f1"),
                filename="taken.txt",
            )
        assert _read_bytes(workspace, project, "library/taken.txt") == b"taken"

    @pytest.mark.parametrize("bad", ["../escape.txt", "sub/dir.txt", "  ", "."])
    def test_unsafe_filenames_rejected_before_any_download(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
        bad: str,
    ) -> None:
        def never_called(url: str, **kwargs: object) -> bytes:
            raise AssertionError("download must not be attempted for an invalid filename")

        monkeypatch.setattr(upload_writes, "fetch_remote_file", never_called)
        with pytest.raises(ValidationError):
            upload_writes.upload_library_file_from_chatgpt(
                conn,
                workspace,
                project,
                file=ChatGPTFileInput(download_url="https://chatgpt.example/x", file_id="f1"),
                filename=bad,
            )

    @pytest.mark.parametrize("filename", ["payload.sh", "notes.md"])
    def test_blocked_extension_rejected_before_any_download(
        self,
        conn: sqlite3.Connection,
        workspace: WorkspaceRoot,
        project: str,
        monkeypatch: pytest.MonkeyPatch,
        filename: str,
    ) -> None:
        """Fail before spending a fetch on bytes the ingestion path would refuse anyway."""

        def never_called(url: str, **kwargs: object) -> bytes:
            raise AssertionError("download must not be attempted for a blocked extension")

        monkeypatch.setattr(upload_writes, "fetch_remote_file", never_called)
        with pytest.raises(UnsupportedFileTypeError):
            upload_writes.upload_library_file_from_chatgpt(
                conn,
                workspace,
                project,
                file=ChatGPTFileInput(download_url="https://chatgpt.example/x", file_id="f1"),
                filename=filename,
            )

    def test_folder_path_outside_library_rejected(
        self, conn: sqlite3.Connection, workspace: WorkspaceRoot, project: str
    ) -> None:
        with pytest.raises(UnknownFolderError):
            upload_writes.upload_library_file_from_chatgpt(
                conn,
                workspace,
                project,
                file=ChatGPTFileInput(download_url="https://chatgpt.example/x", file_id="f1"),
                filename="ok.bin",
                folder_path="canvases",
            )


class TestChatGPTFileInputSchema:
    def test_additional_properties_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            ChatGPTFileInput.model_validate(
                {"download_url": "https://x", "file_id": "f1", "path": "/etc/passwd"}
            )

    def test_only_download_url_and_file_id_required(self) -> None:
        model = ChatGPTFileInput.model_validate({"download_url": "https://x", "file_id": "f1"})
        assert model.mime_type is None
        assert model.file_name is None
