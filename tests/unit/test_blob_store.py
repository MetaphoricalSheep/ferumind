"""Adversarial and behavioural tests for the content-addressed blob store.

The store is entirely path and inode handling, so the digest is treated as
hostile input (``AGENTS.md``: path-security code requires adversarial tests)
and every filesystem refusal is exercised rather than assumed.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from ferumind.core.blob_store import (
    BlobIntegrityError,
    BlobMissingError,
    BlobRef,
    SweepResult,
    adopt_file,
    blob_path,
    blob_store_root,
    link_into,
    store_bytes,
    sweep_unreferenced,
)
from ferumind.core.paths import PathSafetyError

PAYLOAD = b"content-addressed payload\n"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()

#: The signature ``os.link`` is called with inside the module under test.
LinkCall = Callable[..., None]

FILESYSTEMS_WITHOUT_LINKS = [errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP]


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return blob_store_root(tmp_path)


def _inode(path: Path) -> int:
    return path.stat().st_ino


def _links(path: Path) -> int:
    return path.stat().st_nlink


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _refusing_link(number: int) -> LinkCall:
    """An ``os.link`` that fails the way a filesystem without links does."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError(number, os.strerror(number))

    return refuse


class TestDigestValidation:
    """The digest is the only caller-influenced path component in the module."""

    @pytest.mark.parametrize(
        "digest",
        [
            "",
            "nothex" * 10 + "abcd",
            DIGEST.upper(),
            DIGEST[:63],
            DIGEST + "a",
            "../../etc/passwd",
            f"../{DIGEST}",
            f"{DIGEST[:32]}/{DIGEST[:31]}",
            f"/{DIGEST}",
            f"{DIGEST[:63]}\n",
            "a" * 63 + "\x00",
        ],
    )
    def test_only_a_bare_lowercase_sha256_resolves(self, store: Path, digest: str) -> None:
        with pytest.raises(PathSafetyError):
            blob_path(store, digest)

    def test_a_valid_digest_shards_on_its_first_two_characters(self, store: Path) -> None:
        assert blob_path(store, DIGEST) == store.resolve() / DIGEST[:2] / DIGEST

    def test_a_symlinked_blob_name_is_refused_rather_than_followed(
        self, store: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"not mine\n")
        shard = store / DIGEST[:2]
        shard.mkdir(mode=0o700, parents=True)
        (shard / DIGEST).symlink_to(outside)

        with pytest.raises(PathSafetyError):
            blob_path(store, DIGEST)


class TestStoreBytes:
    def test_stores_the_payload_privately_under_its_digest(self, store: Path) -> None:
        ref = store_bytes(store, PAYLOAD)

        blob = blob_path(store, DIGEST)
        assert ref == BlobRef(digest=DIGEST, size_bytes=len(PAYLOAD), linked=True)
        assert blob.read_bytes() == PAYLOAD
        assert _mode(blob) == 0o600
        assert _mode(blob.parent) == 0o700
        assert _mode(store) == 0o700
        assert _mode(store.parent) == 0o700

    def test_storing_the_same_bytes_twice_reuses_the_one_inode(self, store: Path) -> None:
        first = store_bytes(store, PAYLOAD)
        inode = _inode(blob_path(store, first.digest))

        second = store_bytes(store, PAYLOAD)

        assert second == first
        assert _inode(blob_path(store, second.digest)) == inode

    def test_leaves_no_temporary_file_behind(self, store: Path) -> None:
        store_bytes(store, PAYLOAD)

        shard = blob_path(store, DIGEST).parent
        assert [entry.name for entry in shard.iterdir()] == [DIGEST]

    def test_losing_the_publish_race_is_success_not_a_collision(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another writer publishing identical bytes first is the happy path.

        Simulated faithfully: the link *happens* — the blob exists afterwards,
        as it would if a concurrent writer had won — and ``FileExistsError``
        is raised on top of it.
        """
        real_link = os.link
        calls: list[int] = []

        def racing_link(*args: object, **kwargs: object) -> None:
            real_link(*args, **kwargs)  # type: ignore[arg-type] - pass-through wrapper
            calls.append(1)
            if len(calls) == 1:
                raise FileExistsError(errno.EEXIST, "File exists")

        monkeypatch.setattr(os, "link", racing_link)

        ref = store_bytes(store, PAYLOAD)

        assert ref.digest == DIGEST
        blob = blob_path(store, DIGEST)
        assert blob.read_bytes() == PAYLOAD
        assert [entry.name for entry in blob.parent.iterdir()] == [DIGEST]

    def test_a_stored_blob_of_the_wrong_length_is_corruption(self, store: Path) -> None:
        blob = blob_path(store, DIGEST)
        blob.parent.mkdir(mode=0o700, parents=True)
        blob.write_bytes(PAYLOAD + b"tampered")

        with pytest.raises(BlobIntegrityError):
            store_bytes(store, PAYLOAD)

    @pytest.mark.parametrize("number", FILESYSTEMS_WITHOUT_LINKS)
    def test_a_filesystem_without_hardlinks_still_stores_the_bytes(
        self, store: Path, monkeypatch: pytest.MonkeyPatch, number: int
    ) -> None:
        monkeypatch.setattr(os, "link", _refusing_link(number))

        ref = store_bytes(store, PAYLOAD)

        assert ref.linked is False
        assert blob_path(store, DIGEST).read_bytes() == PAYLOAD

    def test_an_unexpected_link_failure_is_not_swallowed(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(os, "link", _refusing_link(errno.ENOSPC))

        with pytest.raises(OSError, match="No space"):
            store_bytes(store, PAYLOAD)

        assert not blob_path(store, DIGEST).exists()


class TestLinkInto:
    def test_a_new_target_shares_the_blobs_inode_and_private_mode(
        self, store: Path, tmp_path: Path
    ) -> None:
        ref = store_bytes(store, PAYLOAD)
        target = tmp_path / "library" / "payload.bin"

        link_into(store, ref, target)

        assert target.read_bytes() == PAYLOAD
        assert _inode(target) == _inode(blob_path(store, DIGEST))
        assert _links(target) == 2
        assert _mode(target) == 0o600

    def test_an_existing_target_with_the_same_mode_is_replaced_by_a_link(
        self, store: Path, tmp_path: Path
    ) -> None:
        ref = store_bytes(store, PAYLOAD)
        target = tmp_path / "library" / "payload.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"stale\n")
        target.chmod(0o600)

        link_into(store, ref, target)

        assert target.read_bytes() == PAYLOAD
        assert _inode(target) == _inode(blob_path(store, DIGEST))

    def test_it_does_not_revert_an_operators_directory_mode(
        self, store: Path, tmp_path: Path
    ) -> None:
        """S-09, one level up: a link target is usually an operator's folder."""
        ref = store_bytes(store, PAYLOAD)
        target = tmp_path / "library" / "payload.bin"
        target.parent.mkdir(parents=True, mode=0o700)
        target.parent.chmod(0o750)

        link_into(store, ref, target)

        assert _mode(target.parent) == 0o750

    def test_a_target_whose_mode_differs_is_copied_so_the_mode_survives(
        self, store: Path, tmp_path: Path
    ) -> None:
        """A hardlink cannot carry a mode of its own.

        ``file_io`` guarantees an existing file's mode survives a rewrite, so
        the space saving loses to that guarantee rather than the other way
        round.
        """
        ref = store_bytes(store, PAYLOAD)
        target = tmp_path / "library" / "shared.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"stale\n")
        target.chmod(0o640)

        link_into(store, ref, target)

        assert target.read_bytes() == PAYLOAD
        assert _mode(target) == 0o640
        assert _inode(target) != _inode(blob_path(store, DIGEST))
        assert _mode(blob_path(store, DIGEST)) == 0o600

    @pytest.mark.parametrize("number", FILESYSTEMS_WITHOUT_LINKS)
    def test_a_filesystem_without_hardlinks_falls_back_to_a_copy(
        self, store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, number: int
    ) -> None:
        ref = store_bytes(store, PAYLOAD)
        target = tmp_path / "library" / "payload.bin"
        monkeypatch.setattr(os, "link", _refusing_link(number))

        link_into(store, ref, target)

        assert target.read_bytes() == PAYLOAD
        assert _inode(target) != _inode(blob_path(store, DIGEST))

    def test_an_unexpected_link_failure_is_not_swallowed(
        self, store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = store_bytes(store, PAYLOAD)
        target = tmp_path / "library" / "payload.bin"
        monkeypatch.setattr(os, "link", _refusing_link(errno.ENOSPC))

        with pytest.raises(OSError, match="No space"):
            link_into(store, ref, target)

    def test_a_failed_rename_leaves_the_old_file_and_no_temporary(
        self, store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Link-then-rename: a crash leaves the old file or the new one."""
        ref = store_bytes(store, PAYLOAD)
        target = tmp_path / "library" / "payload.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"original\n")
        target.chmod(0o600)

        def failing_replace(*args: object, **kwargs: object) -> None:
            raise OSError(errno.EIO, "injected rename failure")

        monkeypatch.setattr(os, "replace", failing_replace)

        with pytest.raises(OSError, match="injected rename failure"):
            link_into(store, ref, target)

        assert target.read_bytes() == b"original\n"
        assert [entry.name for entry in target.parent.iterdir()] == ["payload.bin"]

    def test_a_reference_to_an_unstored_blob_fails_closed(
        self, store: Path, tmp_path: Path
    ) -> None:
        ref = BlobRef(digest=DIGEST, size_bytes=len(PAYLOAD), linked=True)

        with pytest.raises(BlobMissingError):
            link_into(store, ref, tmp_path / "library" / "payload.bin")


class TestAdoptFile:
    def test_an_absent_blob_adopts_the_file_itself_without_copying(
        self, store: Path, tmp_path: Path
    ) -> None:
        source = tmp_path / "library" / "photo.jpg"
        source.parent.mkdir(parents=True)
        source.write_bytes(PAYLOAD)
        source.chmod(0o600)
        inode = _inode(source)

        ref = adopt_file(store, source)

        assert ref == BlobRef(digest=DIGEST, size_bytes=len(PAYLOAD), linked=True)
        assert _inode(blob_path(store, DIGEST)) == inode
        assert _links(source) == 2

    def test_a_present_blob_reclaims_the_files_space(self, store: Path, tmp_path: Path) -> None:
        store_bytes(store, PAYLOAD)
        duplicate = tmp_path / "library" / "copy.jpg"
        duplicate.parent.mkdir(parents=True)
        duplicate.write_bytes(PAYLOAD)
        duplicate.chmod(0o600)
        assert _inode(duplicate) != _inode(blob_path(store, DIGEST))

        ref = adopt_file(store, duplicate)

        assert ref.digest == DIGEST
        assert _inode(duplicate) == _inode(blob_path(store, DIGEST))

    def test_a_filesystem_without_hardlinks_copies_and_says_so(
        self, store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "library" / "photo.jpg"
        source.parent.mkdir(parents=True)
        source.write_bytes(PAYLOAD)
        monkeypatch.setattr(os, "link", _refusing_link(errno.EXDEV))

        ref = adopt_file(store, source)

        assert ref.linked is False
        assert blob_path(store, DIGEST).read_bytes() == PAYLOAD
        assert _inode(source) != _inode(blob_path(store, DIGEST))

    def test_a_stored_blob_of_the_wrong_length_is_corruption(
        self, store: Path, tmp_path: Path
    ) -> None:
        blob = blob_path(store, DIGEST)
        blob.parent.mkdir(mode=0o700, parents=True)
        blob.write_bytes(PAYLOAD + b"tampered")
        source = tmp_path / "photo.jpg"
        source.write_bytes(PAYLOAD)

        with pytest.raises(BlobIntegrityError):
            adopt_file(store, source)


class TestSweepUnreferenced:
    def test_removes_only_what_nothing_else_points_at(self, store: Path, tmp_path: Path) -> None:
        held = store_bytes(store, PAYLOAD)
        orphan = store_bytes(store, b"nobody wants these bytes\n")
        target = tmp_path / "library" / "payload.bin"
        link_into(store, held, target)

        result = sweep_unreferenced(store)

        assert result.removed == 1
        assert result.kept == 1
        assert result.bytes_reclaimed == orphan.size_bytes
        assert blob_path(store, held.digest).is_file()
        assert not blob_path(store, orphan.digest).exists()
        assert target.read_bytes() == PAYLOAD

    def test_an_empty_store_is_not_an_error(self, tmp_path: Path) -> None:
        assert sweep_unreferenced(blob_store_root(tmp_path)) == SweepResult(
            removed=0, kept=0, bytes_reclaimed=0
        )

    def test_it_never_deletes_through_a_symlink_planted_in_the_store(
        self, store: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "someone-elses.txt"
        outside.write_bytes(b"not the store's to delete\n")
        shard = store / DIGEST[:2]
        shard.mkdir(mode=0o700, parents=True)
        planted = shard / DIGEST
        planted.symlink_to(outside)

        result = sweep_unreferenced(store)

        assert result == SweepResult(removed=0, kept=0, bytes_reclaimed=0)
        assert outside.read_bytes() == b"not the store's to delete\n"
        assert planted.is_symlink()

    def test_a_file_that_is_not_named_by_its_digest_is_left_alone(self, store: Path) -> None:
        shard = store / DIGEST[:2]
        shard.mkdir(mode=0o700, parents=True)
        stray = shard / "not-a-digest"
        stray.write_bytes(b"stray\n")

        result = sweep_unreferenced(store)

        assert result == SweepResult(removed=0, kept=0, bytes_reclaimed=0)
        assert stray.is_file()

    def test_a_blob_filed_under_the_wrong_shard_is_left_alone(self, store: Path) -> None:
        misfiled = store / "zz"
        misfiled.mkdir(mode=0o700, parents=True)
        (misfiled / DIGEST).write_bytes(PAYLOAD)

        result = sweep_unreferenced(store)

        assert result == SweepResult(removed=0, kept=0, bytes_reclaimed=0)
        assert (misfiled / DIGEST).is_file()
