"""Shared pytest fixtures for Ferumind tests.

Every fixture builds on ``tmp_path``: a bootstrapped workspace (contract
files installed, current-format marker), a schema-initialized database, and a
seeded project. MCP-level tests get a fully registered tool surface bound to
that workspace via ``mcp_tools``.

:func:`managed_markdown` builds valid managed frontmatter for tests that need
a document on disk. Use it rather than hand-writing a frontmatter block: it
is the single place that knows which keys are currently required, so the next
format bump changes one function instead of every fixture in the suite.
"""

from __future__ import annotations

import io
import math
import random
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bootstrap_workspace import bootstrap  # noqa: E402

from ferumind.core.paths import WorkspaceRoot  # noqa: E402
from ferumind.db.database import Database  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def plain_cli_output() -> object:
    """Assert on CLI text without ANSI styling deciding the answer.

    ``astral-sh/setup-uv`` exports ``FORCE_COLOR=1``, which makes Rich style
    Typer's help panels. It renders an option name as two spans —
    ``\\x1b[1;36m-\\x1b[0m\\x1b[1;36m-workspace\\x1b[0m`` — so ``"--workspace"
    in result.output`` is false on CI and true on a developer machine. Pin the
    environment for the whole session rather than stripping escapes at each
    call site, because every ``in result.output`` assertion in the suite has
    the same exposure.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("NO_COLOR", "1")
        patch.delenv("FORCE_COLOR", raising=False)
        yield


@pytest.fixture
def workspace(tmp_path: Path) -> WorkspaceRoot:
    """A bootstrapped workspace (skeleton + contract + format marker)."""
    ws = tmp_path / "workspace"
    bootstrap(ws, force=False)
    return WorkspaceRoot(ws)


@pytest.fixture
def database(workspace: WorkspaceRoot) -> Database:
    db = Database(workspace / ".ferumind" / "ferumind.sqlite")
    db.init_schema()
    return db


@pytest.fixture
def conn(database: Database) -> Iterator[sqlite3.Connection]:
    connection = database.get_connection()
    yield connection
    connection.close()


@pytest.fixture
def project(conn: sqlite3.Connection, workspace: WorkspaceRoot) -> str:
    """A created project ('demo') with seeded spine and rules."""
    from ferumind.core.project_writes import create_project

    create_project(conn, workspace, key="demo", title="Demo")
    return "demo"


#: Default description for test fixtures. Real enough to pass validation
#: without pretending to be the kind of sentence the contract asks for.
TEST_DESCRIPTION = "Fixture document used by the Ferumind test suite."


def managed_markdown(
    body: str,
    *,
    project_key: str = "demo",
    doc_id: str = "doc_test000000",
    title: str = "Test Document",
    description: str = TEST_DESCRIPTION,
    extra_frontmatter: str = "",
) -> str:
    """Return managed Markdown with valid frontmatter and *body*.

    Centralized so a change to the required-key set is one edit here rather
    than a sweep through every test that happens to write a file.
    """
    lines = [
        "---",
        f"id: {doc_id}",
        "type: document",
        f"project: {project_key}",
        f"title: {title}",
        f"description: {description}",
        "status: active",
    ]
    if extra_frontmatter:
        lines.extend(extra_frontmatter.strip("\n").split("\n"))
    lines.extend(["---", ""])
    return "\n".join(lines) + body.lstrip("\n")


def photograph_like(width: int, height: int, *, seed: int = 4242) -> Image.Image:
    """Build a deterministic image with photograph-like statistics.

    Smooth gradients plus light sensor-style noise. The statistics matter
    for rendition-size assertions: pure noise is incompressible and makes a
    rendition look far worse than any real camera output would, while a
    flat colour compresses so well the fixture cannot reach a realistic
    size at all.
    """
    rng = random.Random(seed)  # noqa: S311 - deterministic fixture data, not a security context
    buffer = bytearray(width * height * 3)
    index = 0
    for y in range(height):
        wave_y = math.sin(y / 331.0)
        tint_y = math.cos(y / 97.0)
        for x in range(width):
            red = 128 + 96 * math.sin(x / 263.0) * wave_y + rng.randint(-18, 18)
            green = 122 + 88 * math.sin((x + y) / 411.0) + tint_y * 30 + rng.randint(-18, 18)
            blue = 141 + 78 * math.cos(x / 187.0) + rng.randint(-18, 18)
            buffer[index] = max(0, min(255, int(red)))
            buffer[index + 1] = max(0, min(255, int(green)))
            buffer[index + 2] = max(0, min(255, int(blue)))
            index += 3
    return Image.frombytes("RGB", (width, height), bytes(buffer))


@pytest.fixture(scope="session")
def large_photo_bytes() -> bytes:
    """A ~5 MB JPEG at phone-camera geometry, generated once per session.

    Built rather than committed so no binary fixture lands in the
    repository, and session-scoped because generating it is the slowest
    thing in the suite.
    """
    buffer = io.BytesIO()
    photograph_like(4032, 3024).save(buffer, format="JPEG", quality=93, optimize=False)
    return buffer.getvalue()
