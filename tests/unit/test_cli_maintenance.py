"""Maintenance-CLI journeys, including ``lint`` and index maintenance (REL-028).

These are commands a user reaches for when
something is already wrong — a stale index, a workspace from an older build, a
library full of oversized photographs — so they are exactly the commands that
must not answer a mistake with a Python traceback.

Two harnesses, chosen per test rather than by habit:

**Subprocess** (``_cli``) is used where the process boundary *is* the thing
under test. A ``CliRunner`` catches the exception a command lets escape and
reports it as ``result.exception``; the rich traceback a real user sees is
rendered by the interpreter's excepthook, which never runs in-process. So the
"concise error, not an internal traceback" acceptance criterion is only
falsifiable in a child process. The same goes for stdout/stderr separation and
for ``ferumind mcp serve``, whose whole contract is that stdout carries nothing
but JSON-RPC.

**In-process** (``CliRunner``) is used for everything else: state-shaped
journeys where the assertion is about the workspace and the summary line, not
about process mechanics. It is an order of magnitude faster, and — unlike the
subprocess tests — its execution is visible to coverage.

That split is deliberate. Subprocess tests run the CLI in a child that
``[tool.coverage.run]`` does not measure (no ``parallel``/``concurrency``
setting), so a suite made entirely of them would exercise these commands
thoroughly while leaving ``cli/main.py`` reported at its old number. Keeping the
in-process tests for the journeys that do not need a real process means the
coverage figure moves for the right reason, and the subprocess tests are spent
only where nothing else can do the job.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ferumind.cli.main import app
from ferumind.core.format import SUPPORTED_FORMAT
from ferumind.core.paths import WorkspaceRoot
from tests.conftest import photograph_like

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap_workspace.py"

runner = CliRunner()


def _run(
    *args: str,
    env_extra: dict[str, str] | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *args* under this interpreter, isolated from the caller's environment."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), **(env_extra or {})}
    return subprocess.run(
        [sys.executable, *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=180,
    )


def _cli(
    *args: str,
    env_extra: dict[str, str] | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run(
        "-c", "from ferumind.cli.main import app; app()", *args, env_extra=env_extra, stdin=stdin
    )


def _bootstrap(path: Path) -> None:
    result = _run(str(BOOTSTRAP), "--workspace", str(path))
    assert result.returncode == 0, result.stderr


def _assert_no_traceback(result: subprocess.CompletedProcess[str]) -> None:
    """A user mistake must not surface as an internal failure.

    Both spellings matter: ``Traceback`` is the interpreter's plain rendering,
    and ``╭─ Traceback`` is Typer's rich one. Exception *class* names are
    checked too, because the rich renderer prints the type and frames without
    ever using the word "Traceback" in the body.
    """
    combined = result.stdout + result.stderr
    for marker in ("Traceback", "Error:", "The above exception", "raise "):
        assert marker not in combined, f"internal failure leaked to the user: {combined!r}"


class TestInfo:
    """``info`` is the first thing anyone runs when a setup looks wrong.

    It takes no ``--workspace`` flag, so ``FERUMIND_WORKSPACE`` is the only way
    to point it somewhere — which makes the env var itself part of the journey.
    """

    def test_reports_an_initialized_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        _bootstrap(workspace)
        result = _cli("info", env_extra={"FERUMIND_WORKSPACE": str(workspace)})

        assert result.returncode == 0, result.stderr
        assert f"Workspace: {workspace}" in result.stdout
        assert f"Workspace format: {SUPPORTED_FORMAT}" in result.stdout
        assert "Projects: (none)" in result.stdout

    def test_names_an_uninitialized_workspace_instead_of_failing(self, tmp_path: Path) -> None:
        """A directory that does not exist yet is the normal pre-bootstrap state.

        It is a status report, not an error: exit 0, and a line naming the
        script that fixes it.
        """
        result = _cli("info", env_extra={"FERUMIND_WORKSPACE": str(tmp_path / "absent")})

        assert result.returncode == 0, result.stderr
        assert "Workspace not initialized" in result.stdout
        assert "bootstrap_workspace.py" in result.stdout
        _assert_no_traceback(result)

    def test_distinguishes_a_missing_marker_from_an_old_format(self, tmp_path: Path) -> None:
        """A directory that exists without ``system/meta.yml`` is not format 0.

        ``read_format`` returns ``None`` for "unknown", and ``info`` must print
        that as unknown rather than substituting a number — the sentinel-default
        mistake that reads as a real, very old workspace.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        result = _cli("info", env_extra={"FERUMIND_WORKSPACE": str(workspace)})

        assert result.returncode == 0, result.stderr
        assert "Workspace format: missing (not an initialized workspace)" in result.stdout

    def test_lists_the_projects_it_finds(self, workspace: WorkspaceRoot, project: str) -> None:
        result = runner.invoke(app, ["info"], env={"FERUMIND_WORKSPACE": str(workspace)})

        assert result.exit_code == 0, result.output
        assert f"Projects: {project}" in result.output


class TestMigrate:
    """``migrate`` is the only sanctioned way a workspace changes format.

    The migrator registry is deliberately empty until a format-breaking change
    needs one, so the reachable paths are the no-op and the refusals. All three
    refusals are user-facing states, and all three must read as sentences.
    """

    def test_no_op_when_already_at_the_supported_format(self, workspace: WorkspaceRoot) -> None:
        result = runner.invoke(app, ["migrate", "--workspace", str(workspace)])

        assert result.exit_code == 0, result.output
        assert (
            f"Workspace already at format {SUPPORTED_FORMAT}; nothing to migrate." in result.output
        )

    def test_dry_run_reports_the_same_no_op_and_writes_nothing(
        self, workspace: WorkspaceRoot
    ) -> None:
        marker = Path(workspace) / "system" / "meta.yml"
        before = marker.read_bytes()

        result = runner.invoke(app, ["migrate", "--dry-run", "--workspace", str(workspace)])

        assert result.exit_code == 0, result.output
        assert "nothing to migrate" in result.output
        assert marker.read_bytes() == before
        assert not list((Path(workspace) / ".ferumind").glob("**/*.tar.gz")), (
            "a dry run must not create a backup tarball"
        )

    def test_refuses_a_workspace_with_no_format_marker(self, tmp_path: Path) -> None:
        """Nothing to migrate *from* is a different failure to "already current".

        Reported as advice — initialize it, or point at the right workspace —
        rather than as an unhandled ``FormatUnsupportedError``.

        CLI-01 moved this refusal earlier. It used to come from ``run_migration``
        *after* ``Database.init_schema`` had already created ``.ferumind/`` on
        the way past; it now comes from the shared workspace guard before any
        database is opened. The message is the guard's rather than migrate's,
        which is why "Cannot migrate:" is no longer the wording — but the
        refusal, the exit code, and the advice are the same, and the stray
        directory is gone.
        """
        workspace = tmp_path / "ws"
        workspace.mkdir()
        result = _cli("migrate", "--workspace", str(workspace))

        assert result.returncode == 1
        assert "not an initialized workspace" in result.stderr
        assert "system/meta.yml" in result.stderr
        assert "bootstrap_workspace.py" in result.stderr
        assert result.stdout == "", "the refusal belongs on stderr"
        assert "Traceback" not in result.stderr
        # The improvement CLI-01 bought: refusing without leaving a database
        # behind. This is what made a mistyped path indistinguishable from a
        # real but empty workspace.
        assert not (workspace / ".ferumind").exists(), "a database was created before refusing"

    def test_refuses_a_workspace_newer_than_this_build(self, workspace: WorkspaceRoot) -> None:
        """The one refusal a user cannot fix by migrating: upgrade the server."""
        marker = Path(workspace) / "system" / "meta.yml"
        marker.write_text(
            marker.read_text(encoding="utf-8").replace(f"format: {SUPPORTED_FORMAT}", "format: 99"),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["migrate", "--workspace", str(workspace)])

        assert result.exit_code == 1
        assert "newer than this build" in result.output
        assert "upgrade the Ferumind server" in result.output


class TestReindex:
    """``reindex`` rebuilds derived state from the Markdown that is authoritative."""

    @pytest.mark.usefixtures("project")
    def test_indexes_every_project_by_default(self, workspace: WorkspaceRoot) -> None:
        result = runner.invoke(app, ["reindex", "--workspace", str(workspace)])

        assert result.exit_code == 0, result.output
        assert "across 1 project(s)." in result.output
        assert "Indexed 0 document(s)" not in result.output, (
            "the seeded spine and rules should have been indexed"
        )

    def test_indexes_one_named_project(self, workspace: WorkspaceRoot, project: str) -> None:
        result = runner.invoke(
            app, ["reindex", "--project", project, "--workspace", str(workspace)]
        )

        assert result.exit_code == 0, result.output
        assert "across 1 project(s)." in result.output

    @pytest.mark.usefixtures("project")
    def test_recovers_an_index_deleted_out_of_band(self, workspace: WorkspaceRoot) -> None:
        """The reason the command exists: the database is disposable, the Markdown is not."""
        from ferumind.core.paths import contained_path

        runner.invoke(app, ["reindex", "--workspace", str(workspace)])
        database = contained_path(workspace, ".ferumind/ferumind.sqlite")
        database.unlink()

        result = runner.invoke(app, ["reindex", "--workspace", str(workspace)])

        assert result.exit_code == 0, result.output
        assert "Indexed 0 document(s)" not in result.output
        assert database.is_file()

    def test_unknown_project_is_a_message_not_a_traceback(self, tmp_path: Path) -> None:
        """``require_project`` raises ``ProjectNotFoundError``; the CLI must catch it.

        It did not, before REL-028: a typo'd ``--project`` printed a full rich
        traceback through ``ferumind.core.registry``.
        """
        workspace = tmp_path / "ws"
        _bootstrap(workspace)
        result = _cli("reindex", "--project", "nope", "--workspace", str(workspace))

        assert result.returncode == 1
        assert "Cannot resolve project: Project 'nope' not found" in result.stderr
        assert "registry.py" not in result.stderr, "an internal frame reached the user"
        assert "Traceback" not in result.stderr


#: Longest edge of the fixture raster. Comfortably over the default 2560 policy
#: edge, so every run has real work to do, and a known number the ``--max-edge``
#: assertion can compare against.
PHOTO_EDGE = 2800


@pytest.fixture(scope="session")
def oversized_jpeg() -> bytes:
    """One real, over-policy JPEG, encoded once for the whole session.

    ``photograph_like`` is pure Python and costs roughly a second per
    megapixel. Every test below needs the *same* oversized photograph, so
    generating it per test bought nothing and dominated the file's runtime.
    """
    buffer = io.BytesIO()
    photograph_like(PHOTO_EDGE, PHOTO_EDGE * 3 // 4).save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def _place(project_root: Path, name: str, data: bytes) -> Path:
    """Drop *data* into a project's library, creating the folder if needed."""
    path = project_root / "library" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class TestCompressImages:
    """``compress-images`` re-applies the storage policy to files already on disk.

    Every test here uses a real raster: the command's whole job is decoding,
    resizing, and re-encoding, and an empty fixture would prove none of it.
    """

    def test_rewrites_an_oversized_photograph(
        self, workspace: WorkspaceRoot, project: str, oversized_jpeg: bytes
    ) -> None:
        photo = _place(Path(workspace) / "projects" / project, "big.jpg", oversized_jpeg)
        before = photo.stat().st_size

        result = runner.invoke(app, ["compress-images", "--workspace", str(workspace)])

        assert result.exit_code == 0, result.output
        assert "1 file(s) rewritten across 1 project(s)." in result.output
        assert "policy: max_edge=2560 quality=85" in result.output
        assert photo.stat().st_size < before

    def test_second_run_changes_nothing(
        self, workspace: WorkspaceRoot, project: str, oversized_jpeg: bytes
    ) -> None:
        """Idempotence is a documented property, so it is asserted, not assumed."""
        photo = _place(Path(workspace) / "projects" / project, "big.jpg", oversized_jpeg)
        runner.invoke(app, ["compress-images", "--workspace", str(workspace)])
        converged = photo.read_bytes()

        result = runner.invoke(app, ["compress-images", "--workspace", str(workspace)])

        assert result.exit_code == 0, result.output
        assert "0 file(s) rewritten" in result.output
        assert photo.read_bytes() == converged

    def test_dry_run_reports_the_saving_without_writing(
        self, workspace: WorkspaceRoot, project: str, oversized_jpeg: bytes
    ) -> None:
        photo = _place(Path(workspace) / "projects" / project, "big.jpg", oversized_jpeg)
        before = photo.read_bytes()

        result = runner.invoke(app, ["compress-images", "--dry-run", "--workspace", str(workspace)])

        assert result.exit_code == 0, result.output
        assert "Would reclaim" in result.output
        assert "1 file(s) rewritten" in result.output
        assert photo.read_bytes() == before, "a dry run must not touch the file"

    def test_verbose_names_each_file(
        self, workspace: WorkspaceRoot, project: str, oversized_jpeg: bytes
    ) -> None:
        _place(Path(workspace) / "projects" / project, "big.jpg", oversized_jpeg)

        result = runner.invoke(
            app, ["compress-images", "--verbose", "--dry-run", "--workspace", str(workspace)]
        )

        assert result.exit_code == 0, result.output
        assert "library/big.jpg" in result.output

    def test_scoped_to_one_project(self, workspace: WorkspaceRoot, project: str) -> None:
        result = runner.invoke(
            app, ["compress-images", "--project", project, "--workspace", str(workspace)]
        )

        assert result.exit_code == 0, result.output
        assert "across 1 project(s)." in result.output

    def test_max_edge_override_is_honored(
        self, workspace: WorkspaceRoot, project: str, oversized_jpeg: bytes
    ) -> None:
        from PIL import Image

        photo = _place(Path(workspace) / "projects" / project, "big.jpg", oversized_jpeg)

        result = runner.invoke(
            app, ["compress-images", "--max-edge", "1024", "--workspace", str(workspace)]
        )

        assert result.exit_code == 0, result.output
        assert "policy: max_edge=1024" in result.output
        with Image.open(photo) as reopened:
            assert max(reopened.size) == 1024

    def test_an_undecodable_file_is_skipped_not_failed(
        self, workspace: WorkspaceRoot, project: str, oversized_jpeg: bytes
    ) -> None:
        """A ``.jpg`` that is not an image is a skip, and the run still succeeds.

        Worth pinning because the opposite is the intuitive guess: the pipeline
        reports ``not_a_decodable_image`` rather than raising, so garbage in the
        library does not make an otherwise clean maintenance run exit non-zero.
        The good file beside it must still be rewritten.
        """
        library = Path(workspace) / "projects" / project
        good = _place(library, "good.jpg", oversized_jpeg)
        junk = b"this is not a JPEG"
        broken = _place(library, "broken.jpg", junk)
        before = good.stat().st_size

        result = runner.invoke(app, ["compress-images", "--verbose", "--workspace", str(workspace)])

        assert result.exit_code == 0, result.output
        assert "1 changed, 1 unchanged, 0 failed" in result.output
        assert "library/broken.jpg: unchanged (not_a_decodable_image)" in result.output
        assert good.stat().st_size < before, "one bad file must not stop the pass"
        assert broken.read_bytes() == junk, "an undecodable file is left byte-identical"

    def test_a_file_that_fails_to_compress_exits_non_zero(
        self,
        workspace: WorkspaceRoot,
        project: str,
        oversized_jpeg: bytes,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The non-zero exit is the only signal a script has that a run was incomplete.

        Nothing in a realistic workspace makes the codec *raise* — malformed
        input is reported as a skip (above), and the ``OSError`` arm needs an
        unreadable file, which is not reproducible as root. So the failure is
        injected at the one seam that produces it. ``image_maintenance`` calls
        ``compress_image_for_storage`` as a module global, so patching it there
        is live; ``codec_calls`` proves it fired rather than the assertions
        passing on a disarmed patch.
        """
        from ferumind.core import image_maintenance

        codec_calls: list[str] = []

        def exploding_codec(raw: bytes, *, policy: object) -> object:
            codec_calls.append("called")
            raise ValueError("decoder blew up")

        monkeypatch.setattr(image_maintenance, "compress_image_for_storage", exploding_codec)
        photo = _place(Path(workspace) / "projects" / project, "big.jpg", oversized_jpeg)

        result = runner.invoke(app, ["compress-images", "--workspace", str(workspace)])

        assert codec_calls, "the injected failure never fired"
        assert result.exit_code == 1
        assert "0 changed, 0 unchanged, 1 failed" in result.output
        assert "library/big.jpg: ValueError: decoder blew up" in result.output
        assert "1 file(s) failed; see errors above." in result.output
        assert photo.read_bytes() == oversized_jpeg, "a failed file must be left untouched"

    @pytest.mark.parametrize(
        ("flag", "value", "expected"),
        [
            ("--max-edge", "5", "max_edge must be between"),
            ("--quality", "500", "quality must be between"),
        ],
    )
    def test_a_bad_override_fails_before_any_file_is_touched(
        self, tmp_path: Path, flag: str, value: str, expected: str, oversized_jpeg: bytes
    ) -> None:
        """The policy is validated up front on purpose; that has to reach the user.

        Run in a subprocess because the point is what a mistyped flag *looks
        like*: before REL-028 it was a ``ValidationError`` traceback out of
        ``core/images.py``.
        """
        workspace = tmp_path / "ws"
        _bootstrap(workspace)
        create = _cli("project", "create", "demo", "--title", "D", "--workspace", str(workspace))
        assert create.returncode == 0, create.stderr
        photo = _place(workspace / "projects" / "demo", "big.jpg", oversized_jpeg)
        before = photo.read_bytes()

        result = _cli("compress-images", flag, value, "--workspace", str(workspace))

        assert result.returncode == 1
        assert f"Cannot compress images: image {expected}" in result.stderr
        assert "Traceback" not in result.stderr
        assert photo.read_bytes() == before, "validation must precede the first rewrite"

    def test_unknown_project_is_a_message_not_a_traceback(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        _bootstrap(workspace)
        result = _cli("compress-images", "--project", "nope", "--workspace", str(workspace))

        assert result.returncode == 1
        assert "Cannot resolve project: Project 'nope' not found" in result.stderr
        assert "Traceback" not in result.stderr


def test_mcp_serve_speaks_jsonrpc_on_stdout_and_exits_when_stdin_closes(tmp_path: Path) -> None:
    """``ferumind mcp serve`` is the command every MCP client actually runs.

    Its contract is narrow and easy to break by accident: stdout carries
    JSON-RPC and nothing else, so a stray ``print`` — or a logging handler
    defaulting to stdout — corrupts the stream for every client. ``_configure``
    exists to prevent that, which is why this runs at DEBUG: the noisiest
    setting must still leave stdout clean.

    Only reproducible in a subprocess; in-process there are no real streams to
    contaminate.
    """
    workspace = tmp_path / "ws"
    _bootstrap(workspace)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "rel-028", "version": "0"},
        },
    }

    result = _cli(
        "mcp",
        "serve",
        "--workspace",
        str(workspace),
        stdin=json.dumps(request) + "\n",
        env_extra={"FERUMIND_LOG_LEVEL": "DEBUG"},
    )

    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, "the server answered nothing"
    for line in lines:
        parsed = json.loads(line)  # any non-JSON line on stdout fails here
        assert parsed["jsonrpc"] == "2.0"
    assert json.loads(lines[0])["result"]["serverInfo"]["name"] == "Ferumind"


class TestUninitializedWorkspaceIsRefused:
    """CLI-01: a mistyped ``--workspace`` must fail, not invent a workspace.

    Every maintenance command reaches ``Database.init_schema``, which creates
    the directory and an empty SQLite file on the way past. Before this guard,
    ``reindex --workspace /tmp/tpyo`` printed ``Indexed 0 document(s) across 0
    project(s).`` and exited 0 — a confident success for a path that had never
    existed, because a freshly created database is indistinguishable from a
    correctly reindexed empty workspace.

    The assertions that matter are the filesystem ones. A non-zero exit alone
    would still pass if the command created the workspace and *then* failed.
    """

    MAINTENANCE = ("reindex", "compress-images", "migrate", "verify-index", "lint")

    @pytest.mark.parametrize("command", MAINTENANCE)
    def test_a_missing_workspace_is_refused_without_creating_it(
        self, command: str, tmp_path: Path
    ) -> None:
        target = tmp_path / "tpyo"

        result = _cli(command, "--workspace", str(target))

        assert result.returncode != 0, result.stdout
        assert not target.exists(), f"{command} created the workspace it was meant to refuse"
        assert "No workspace at" in result.stderr
        _assert_no_traceback(result)

    @pytest.mark.parametrize("command", MAINTENANCE)
    def test_an_unbootstrapped_directory_is_not_a_workspace(
        self, command: str, tmp_path: Path
    ) -> None:
        """Existing is not the same as initialized.

        A bare directory has no ``system/meta.yml``. ``read_format`` returns
        ``None`` for that and documents that callers must not substitute a
        number, so the CLI refuses rather than treating it as a workspace.
        """
        target = tmp_path / "not-a-workspace"
        target.mkdir()

        result = _cli(command, "--workspace", str(target))

        assert result.returncode != 0, result.stdout
        assert not (target / ".ferumind").exists(), "a database was created anyway"
        assert "not an initialized workspace" in result.stderr
        _assert_no_traceback(result)

    @pytest.mark.parametrize("command", MAINTENANCE)
    def test_a_real_workspace_is_still_accepted(self, command: str, tmp_path: Path) -> None:
        """The guard must not cost a working command its happy path."""
        workspace = tmp_path / "workspace"
        _bootstrap(workspace)

        result = _cli(command, "--workspace", str(workspace))

        assert result.returncode == 0, result.stderr
        _assert_no_traceback(result)


class TestInfoTakesWorkspace:
    """CLI-01: ``info`` was the only command that could not be pointed anywhere.

    It is also the command whose entire job is describing a workspace, so the
    gap meant the obvious way to diagnose a wrong ``FERUMIND_WORKSPACE`` was
    the one thing unavailable.
    """

    def test_info_accepts_the_workspace_flag(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        _bootstrap(workspace)

        result = _cli("info", "--workspace", str(workspace))

        assert result.returncode == 0, result.stderr
        assert str(workspace) in result.stdout

    def test_info_reports_a_missing_workspace_rather_than_creating_one(
        self, tmp_path: Path
    ) -> None:
        """``info`` reports; it does not refuse and it does not create.

        Deliberately unlike the maintenance commands: saying "this is not a
        workspace" is the answer it exists to give, so it exits 0.
        """
        target = tmp_path / "absent"

        result = _cli("info", "--workspace", str(target))

        assert result.returncode == 0, result.stderr
        assert not target.exists(), "info created the workspace it was asked about"
        assert "not initialized" in result.stdout


class TestPrune:
    """``prune`` reclaims Ferumind's own derived state, and defaults to saying so.

    The command is pointed at a workspace that is the only copy of somebody's
    knowledge, so the tests that matter most are the ones asserting it did
    nothing: without ``--apply`` a run must leave the tree byte-identical.
    """

    def _aged_snapshot(self, workspace: WorkspaceRoot, project: str) -> Path:
        """Put one long-expired snapshot in the project, registry row included."""
        import sqlite3
        from datetime import UTC, datetime, timedelta

        from ferumind.core.paths import contained_project_root
        from ferumind.core.snapshots import create_snapshot, new_snapshot_id

        project_root = contained_project_root(workspace, project)
        snapshot_id = new_snapshot_id()
        directory = create_snapshot(
            project_root,
            project_key=project,
            target_path="canvases/plan.md",
            before_content="superseded text\n",
            after_content=None,
            reason="test_prune",
            snapshot_id=snapshot_id,
        )
        stamp = (datetime.now(UTC) - timedelta(days=400)).strftime("%Y%m%dT%H%M%S")
        aged = directory.with_name(f"{stamp}-{snapshot_id}")
        directory.rename(aged)
        conn = sqlite3.connect(Path(workspace) / ".ferumind" / "ferumind.sqlite")
        try:
            conn.execute(
                "INSERT INTO snapshots (id, project_key, target_path, snapshot_dir, reason, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    project,
                    "canvases/plan.md",
                    str(aged),
                    "test_prune",
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return aged

    def test_the_default_run_deletes_nothing(self, workspace: WorkspaceRoot, project: str) -> None:
        snapshot = self._aged_snapshot(workspace, project)

        result = runner.invoke(
            app,
            [
                "prune",
                "--keep",
                "snapshot-days=1",
                "--keep",
                "recent-snapshots=0",
                "--workspace",
                str(workspace),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Nothing was deleted" in result.output
        assert "snapshots (demo): 1 of 1" in result.output
        assert snapshot.is_dir()

    def test_apply_reclaims_and_says_what_it_took(
        self, workspace: WorkspaceRoot, project: str
    ) -> None:
        snapshot = self._aged_snapshot(workspace, project)

        result = runner.invoke(
            app,
            [
                "prune",
                "--apply",
                "--keep",
                "snapshot-days=1",
                "--keep",
                "recent-snapshots=0",
                "--workspace",
                str(workspace),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "snapshots (demo): 1 of 1" in result.output
        assert "after VACUUM" in result.output
        assert not snapshot.exists()

    def test_a_second_apply_finds_nothing_left(
        self, workspace: WorkspaceRoot, project: str
    ) -> None:
        self._aged_snapshot(workspace, project)
        arguments = [
            "prune",
            "--apply",
            "--keep",
            "snapshot-days=1",
            "--keep",
            "recent-snapshots=0",
            "--workspace",
            str(workspace),
        ]
        runner.invoke(app, arguments)

        result = runner.invoke(app, arguments)

        assert result.exit_code == 0, result.output
        assert "Reclaimed 0 item(s)" in result.output

    @pytest.mark.usefixtures("project")
    def test_an_unknown_project_is_a_message_not_a_traceback(
        self, workspace: WorkspaceRoot
    ) -> None:
        result = _cli("prune", "--project", "nope", "--workspace", str(workspace))

        assert result.returncode == 1
        assert "Cannot prune" in result.stderr
        assert "Nothing was deleted" in result.stderr
        assert "Traceback" not in result.stderr

    @pytest.mark.usefixtures("project")
    def test_an_out_of_range_override_is_refused(self, workspace: WorkspaceRoot) -> None:
        result = _cli("prune", "--keep", "snapshot-days=0", "--workspace", str(workspace))

        assert result.returncode == 1
        assert "Cannot prune" in result.stderr
        assert "Traceback" not in result.stderr
