"""Installing ``product/contract/`` into a workspace's ``system/`` tree.

One installer, two callers. ``scripts/bootstrap_workspace.py`` runs it when a
workspace is created (and again, forcing, on ``--force``); a format migrator
runs it so a migrated workspace ends up with the same contract a freshly
bootstrapped one gets. Before this existed the bootstrap script owned the only
copy of the map, which is how EPI-01 found an installed
``system/prompts/bootstrap.md`` that had gone stale against its source: nothing
reconciled the installed copy with the contract it came from, and no migration
could, because the map was in a script core could not import.

The contract source lives in the repository rather than inside the package, so
:func:`contract_source_dir` returns ``None`` when Ferumind is running from an
installed distribution instead of a checkout. Callers must handle that rather
than guess a path — a migration that silently skipped the contract install
would leave a workspace that reports the new format while carrying the old
contract, which is worse than refusing.
"""

from __future__ import annotations

from pathlib import Path

from ferumind.core.file_io import atomic_write_text
from ferumind.core.paths import PathSafetyError, WorkspaceRoot, contained_path

#: ``product/contract`` source path → workspace-relative destination.
CONTRACT_INSTALLS: dict[str, str] = {
    "rules/00-contract.md": "system/rules/00-contract.md",
    "rules/10-editing.md": "system/rules/10-editing.md",
    "rules/20-memory.md": "system/rules/20-memory.md",
    "rules/30-reminders.md": "system/rules/30-reminders.md",
    "bootstrap.md": "system/prompts/bootstrap.md",
    "templates/spine.md": "system/templates/spine.md",
    "templates/project-rules.md": "system/templates/project-rules.md",
    # Ferumind skills: on-demand procedure text, listed explicitly for the same
    # reason the rules are — a workspace must fail loudly on a missing source
    # rather than silently install whatever a glob happened to find.
    "skills/distilling-durable-knowledge.md": "system/skills/distilling-durable-knowledge.md",
}


def contract_source_dir() -> Path | None:
    """Return the repository's ``product/contract`` directory, or ``None``.

    ``None`` means this build is not running from a source checkout, so there
    is no contract to install from.
    """
    candidate = Path(__file__).resolve().parents[3] / "product" / "contract"
    return candidate if candidate.is_dir() else None


def contract_source_failures(source: Path) -> list[str]:
    """Return deterministic reasons *source* cannot supply the full contract.

    A migration must establish that every input exists, is symlink-free, and
    is readable UTF-8 before it changes the workspace. Checking only the
    source directory lets a missing late entry fail after earlier contract
    files have already been overwritten.
    """
    _contents, failures = _read_contract_sources(source)
    return failures


def _read_contract_sources(source: Path) -> tuple[dict[str, str], list[str]]:
    contents: dict[str, str] = {}
    failures: list[str] = []
    if not source.is_dir():
        return contents, [f"Contract source directory is missing: {source}"]
    if source.is_symlink():
        return contents, [f"Contract source directory must not be a symlink: {source}"]

    for source_rel in CONTRACT_INSTALLS:
        try:
            source_path = contained_path(source, source_rel)
            if not source_path.is_file():
                failures.append(f"Contract source missing: {source_rel}")
                continue
            contents[source_rel] = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, PathSafetyError) as exc:
            failures.append(f"Contract source unreadable: {source_rel} ({type(exc).__name__})")
    return contents, failures


def install_contract(
    workspace: WorkspaceRoot,
    *,
    source: Path,
    force: bool,
) -> list[str]:
    """Install the contract files into *workspace*, returning what was written.

    ``force=False`` leaves existing files alone, which is what a bootstrap of
    an already-populated workspace wants. ``force=True`` overwrites them, which
    is what a format migration wants: the contract is Ferumind's own text, not
    the user's, and a migration exists precisely to bring it forward.

    Workspace identity, the project registry, and local operator guidance are
    **not** in :data:`CONTRACT_INSTALLS` and are never touched here.
    """
    contents, failures = _read_contract_sources(source)
    if failures:
        raise FileNotFoundError("; ".join(failures))

    destinations = {
        source_rel: contained_path(workspace, dest_rel)
        for source_rel, dest_rel in CONTRACT_INSTALLS.items()
    }
    written: list[str] = []
    for source_rel, dest_rel in CONTRACT_INSTALLS.items():
        destination = destinations[source_rel]
        content = contents[source_rel]
        if destination.exists() and (
            not force or destination.read_text(encoding="utf-8") == content
        ):
            continue
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write_text(destination, content)
        destination.chmod(0o600)
        written.append(dest_rel)
    return written
