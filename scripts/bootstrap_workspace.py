#!/usr/bin/env python3
"""Initialize a Ferumind workspace.

Creates the workspace skeleton per product/spec-mcp.md §2 and installs the
contract content from product/contract/ (the source of record) into
workspace/system/. Existing files are never overwritten unless --force.
Even with --force, workspace identity and the project registry are preserved.

Usage:
    uv run python scripts/bootstrap_workspace.py
    uv run python scripts/bootstrap_workspace.py --workspace /custom/path
    FERUMIND_WORKSPACE=/custom/path uv run python scripts/bootstrap_workspace.py
    uv run python scripts/bootstrap_workspace.py --force
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from ferumind.core.config import load_config
from ferumind.core.contract_install import install_contract
from ferumind.core.errors import FormatUnsupportedError
from ferumind.core.file_io import atomic_write_text
from ferumind.core.format import SUPPORTED_FORMAT, read_format
from ferumind.core.locks import acquire_workspace_lock
from ferumind.core.paths import WorkspaceRoot, contained_path

#: A bootstrapped workspace is a current-format workspace, always. Deriving
#: this from core rather than restating it means a format bump cannot leave
#: bootstrap writing a marker the server no longer serves.
FORMAT_VERSION = SUPPORTED_FORMAT

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_DIR = REPO_ROOT / "product" / "contract"

DIRECTORIES: list[str] = [
    "system",
    "system/rules",
    "system/skills",
    "system/prompts",
    "system/templates",
    "system/schemas",
    "projects",
]

AGENTS_POINTER = """\
# Ferumind workspace

This is a Ferumind workspace. It is live user data, not source code.

- The rules that govern how agents work here live in `system/rules/`
  (read them in lexical order; project rules in `projects/<key>/rules/`
  layer on top).
- The bootstrap prompt for connecting a chat agent is
  `system/prompts/bootstrap.md`.
- Connected agents should call the `get_context` MCP tool rather than
  reading these files directly — it delivers the merged rules, spine, and
  document map for a project.

Do not edit files under `system/` casually: they are loaded into every chat
on every project.
"""


def _write(
    workspace: Path,
    relative_path: str,
    content: str,
    force: bool,
    written: list[str],
) -> None:
    path = contained_path(workspace, relative_path)
    if path.exists() and not force:
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_text(path, content)
    path.chmod(0o600)
    written.append(str(path))


def bootstrap(workspace: Path, force: bool) -> list[str]:
    workspace = workspace.resolve()
    existing_content = workspace.is_dir() and any(workspace.iterdir())
    if existing_content:
        existing_root = WorkspaceRoot(workspace)
        found_format = read_format(existing_root)
        if found_format != FORMAT_VERSION:
            raise FormatUnsupportedError(
                "Refusing to bootstrap an existing workspace whose format "
                f"is not {FORMAT_VERSION!r}; run `ferumind migrate` explicitly",
                details={
                    "found_format": found_format,
                    "supported_format": FORMAT_VERSION,
                },
            )

    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace.chmod(0o700)
    workspace_root = WorkspaceRoot(workspace)
    with acquire_workspace_lock(workspace_root):
        marker = contained_path(workspace_root, "system/meta.yml")
        if marker.exists():
            found_format = read_format(workspace_root)
            if found_format != FORMAT_VERSION:
                raise FormatUnsupportedError(
                    "Refusing to bootstrap a workspace whose format "
                    f"is not {FORMAT_VERSION!r}; run `ferumind migrate` explicitly",
                    details={
                        "found_format": found_format,
                        "supported_format": FORMAT_VERSION,
                    },
                )
        elif existing_content or any(
            entry.name != ".ferumind" for entry in workspace_root.iterdir()
        ):
            raise FormatUnsupportedError(
                "Refusing to bootstrap an existing workspace without a format marker; "
                "run `ferumind migrate` explicitly",
                details={
                    "found_format": None,
                    "supported_format": FORMAT_VERSION,
                },
            )

        written: list[str] = []
        for rel in DIRECTORIES:
            directory = contained_path(workspace_root, rel)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)

        meta = (
            "# Ferumind workspace metadata. Managed by Ferumind; do not edit by hand.\n"
            f"format: {FORMAT_VERSION}\n"
            f'created: "{datetime.now(UTC).date().isoformat()}"\n'
        )
        # These files carry workspace identity, live project registration, and
        # local operator guidance. Contract refreshes must never reset them.
        _write(workspace_root, "system/meta.yml", meta, False, written)
        _write(workspace_root, "system/projects.yml", "projects: {}\n", False, written)
        _write(workspace_root, "AGENTS.md", AGENTS_POINTER, False, written)

        # Shared with the format migrators, so a migrated workspace and a
        # freshly bootstrapped one converge on the same contract.
        written.extend(
            str(contained_path(workspace_root, rel))
            for rel in install_contract(workspace_root, source=CONTRACT_DIR, force=force)
        )

        return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help=(
            "Workspace directory to initialize "
            "(default: $FERUMIND_WORKSPACE, else <repo>/workspace)"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refresh contract rules/templates; preserve workspace identity and registry",
    )
    args = parser.parse_args()

    # Same resolution order as the CLI and the MCP server: explicit flag, then
    # FERUMIND_WORKSPACE, then the repo-local default. This script used to
    # hardcode the repo default, so anyone who exported FERUMIND_WORKSPACE and
    # ran bootstrap silently initialized the *repo's* workspace instead of
    # their own.
    #
    # A relative value resolves against the repo root, not the caller's cwd —
    # matching cli.main._workspace_root, so `bootstrap` and `ferumind info`
    # never disagree about which workspace they mean.
    configured = args.workspace or load_config().workspace_path
    workspace: Path = (
        configured.resolve() if configured.is_absolute() else (REPO_ROOT / configured).resolve()
    )
    written = bootstrap(workspace, args.force)
    if written:
        print(f"Initialized {workspace}:")
        for path in written:
            print(f"  {path}")
    else:
        print(f"{workspace} already up to date (use --force to overwrite).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
