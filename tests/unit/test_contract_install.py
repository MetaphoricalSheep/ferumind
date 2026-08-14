"""Contract installation must validate all inputs before its first write."""

from __future__ import annotations

from pathlib import Path

import pytest

from ferumind.core.contract_install import (
    CONTRACT_INSTALLS,
    contract_source_failures,
    install_contract,
)
from ferumind.core.paths import WorkspaceRoot


def _contract_source(root: Path, *, omit: str | None = None) -> Path:
    source = root / "contract"
    for source_rel in CONTRACT_INSTALLS:
        if source_rel == omit:
            continue
        path = source / source_rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"source for {source_rel}\n", encoding="utf-8")
    return source


def test_contract_install_reads_every_source_before_writing(
    tmp_path: Path,
) -> None:
    workspace = WorkspaceRoot(tmp_path / "workspace")
    workspace.mkdir()
    missing = next(reversed(CONTRACT_INSTALLS))
    source = _contract_source(tmp_path, omit=missing)

    with pytest.raises(FileNotFoundError, match=missing):
        install_contract(workspace, source=source, force=True)

    for destination_rel in CONTRACT_INSTALLS.values():
        assert not (workspace / destination_rel).exists()


def test_contract_source_validation_reports_every_missing_input(tmp_path: Path) -> None:
    source = _contract_source(tmp_path)
    missing = list(CONTRACT_INSTALLS)[:2]
    for source_rel in missing:
        (source / source_rel).unlink()

    failures = contract_source_failures(source)

    assert len(failures) == 2
    assert all(any(source_rel in failure for failure in failures) for source_rel in missing)


def test_contract_source_validation_rejects_symlinked_input(tmp_path: Path) -> None:
    source = _contract_source(tmp_path)
    source_rel = next(iter(CONTRACT_INSTALLS))
    source_path = source / source_rel
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    source_path.unlink()
    source_path.symlink_to(outside)

    failures = contract_source_failures(source)

    assert len(failures) == 1
    assert source_rel in failures[0]
    assert "PathSafetyError" in failures[0]
