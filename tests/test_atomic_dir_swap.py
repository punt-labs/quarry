"""Unit tests for :mod:`quarry.atomic_dir_swap`.

``tests/test_skills_install.py`` already exercises this through
:class:`~quarry.skills_source.SkillSource.deposit`'s real-world call pattern
(``TestDepositAtomicity``, ``TestOrphanSweep``); these tests pin the
primitive's own behavior at the unit level, independent of skills, hashing,
or manifests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.atomic_dir_swap import AtomicDirectorySwap, AtomicSwapError


def _populate(tmp: Path, content: str = "hello") -> None:
    tmp.mkdir(parents=True)
    (tmp / "file.txt").write_text(content, encoding="utf-8")


class TestPrepareAndCommit:
    def test_a_fresh_target_is_created_from_the_populated_tmp(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "skills" / "demo"
        swap = AtomicDirectorySwap("demo", target)

        tmp = swap.prepare()
        _populate(tmp)
        swap.commit(tmp)

        assert (target / "file.txt").read_text(encoding="utf-8") == "hello"
        assert not tmp.exists()

    def test_committing_over_an_existing_target_replaces_it(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "skills" / "demo"
        _populate(target, "v1")
        swap = AtomicDirectorySwap("demo", target)

        tmp = swap.prepare()
        _populate(tmp, "v2")
        swap.commit(tmp)

        assert (target / "file.txt").read_text(encoding="utf-8") == "v2"
        # no leftover backup sibling after a successful swap
        assert list(target.parent.glob(".demo.bak-*")) == []

    def test_prepare_creates_the_targets_parent_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c" / "demo"
        swap = AtomicDirectorySwap("demo", target)

        swap.prepare()

        assert target.parent.is_dir()


class TestOrphanSweep:
    def test_prepare_sweeps_a_stale_tmp_and_bak_sibling(self, tmp_path: Path) -> None:
        parent = tmp_path / "skills"
        parent.mkdir()
        orphan_tmp = parent / ".demo.tmp-999999"
        orphan_tmp.mkdir()
        orphan_bak = parent / ".demo.bak-999999"
        orphan_bak.mkdir()

        AtomicDirectorySwap("demo", parent / "demo").prepare()

        assert not orphan_tmp.exists()
        assert not orphan_bak.exists()

    def test_prepare_sweeps_a_stale_backup_file_not_a_directory(
        self, tmp_path: Path
    ) -> None:
        parent = tmp_path / "skills"
        parent.mkdir()
        stray_file = parent / ".demo.bak-123"
        stray_file.write_text("not a dir\n", encoding="utf-8")

        AtomicDirectorySwap("demo", parent / "demo").prepare()

        assert not stray_file.exists()

    def test_a_glob_metacharacter_in_the_name_does_not_sweep_unrelated_siblings(
        self, tmp_path: Path
    ) -> None:
        """A target literally named ``*`` must not turn the sweep's glob
        pattern into a wildcard matching -- and deleting -- an unrelated
        sibling's own orphaned temp directory."""
        parent = tmp_path / "skills"
        parent.mkdir()
        unrelated = parent / ".demo-a.tmp-999999"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("keep\n", encoding="utf-8")

        AtomicDirectorySwap("*", parent / "*").prepare()

        assert unrelated.exists()


class TestSwapFailureRecovery:
    def test_a_failed_swap_restores_the_previous_occupant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bug class 1: an interrupted rename must never leave the target gone."""
        target = tmp_path / "skills" / "demo"
        _populate(target, "v1")
        swap = AtomicDirectorySwap("demo", target)
        tmp = swap.prepare()
        _populate(tmp, "v2")

        real_rename = Path.rename

        def _boom(self: Path, target_path: str | Path) -> Path:
            if self.name.startswith(".demo.tmp-"):
                msg = "disk full"
                raise OSError(msg)
            return real_rename(self, target_path)

        monkeypatch.setattr(Path, "rename", _boom)

        with pytest.raises(OSError, match="disk full"):
            swap.commit(tmp)

        assert target.is_dir()
        assert (target / "file.txt").read_text(encoding="utf-8") == "v1"
        assert list(target.parent.glob(".demo.*")) == []

    def test_fresh_target_swap_failure_leaves_no_target_and_no_orphans(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No prior occupant to restore -- the original error propagates plain."""
        target = tmp_path / "skills" / "demo"
        swap = AtomicDirectorySwap("demo", target)
        tmp = swap.prepare()
        _populate(tmp)

        real_rename = Path.rename

        def _boom(self: Path, target_path: str | Path) -> Path:
            if self.name.startswith(".demo.tmp-"):
                msg = "disk full"
                raise OSError(msg)
            return real_rename(self, target_path)

        monkeypatch.setattr(Path, "rename", _boom)

        with pytest.raises(OSError, match="disk full"):
            swap.commit(tmp)

        assert not target.exists()
        assert list(target.parent.glob(".demo.*")) == []

    def test_double_failure_raises_a_chained_error_naming_the_backup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Swap fails AND restoring the backup also fails: the backup path is
        preserved and named in a chained :class:`AtomicSwapError`, never a
        silent loss of the last-known-good copy."""
        target = tmp_path / "skills" / "demo"
        _populate(target, "v1")
        swap = AtomicDirectorySwap("demo", target)
        tmp = swap.prepare()
        _populate(tmp, "v2")

        real_rename = Path.rename

        def _boom(self: Path, target_path: str | Path) -> Path:
            if self.name.startswith((".demo.tmp-", ".demo.bak-")):
                msg = "disk full"
                raise OSError(msg)
            return real_rename(self, target_path)

        monkeypatch.setattr(Path, "rename", _boom)

        with pytest.raises(AtomicSwapError) as excinfo:
            swap.commit(tmp)

        assert str(target.parent / ".demo.bak-") in str(excinfo.value.backup)
        assert excinfo.value.backup.is_dir()  # the last-known-good copy survives
        assert excinfo.value.__cause__ is not None

    def test_a_failed_backup_rename_cleans_up_the_freshly_populated_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure renaming the live target aside to its backup name happens
        BEFORE tmp is ever swapped in; the freshly-populated tmp must still be
        cleaned up immediately, not left for the next swap's orphan sweep to
        eventually find."""
        target = tmp_path / "skills" / "demo"
        _populate(target, "v1")
        swap = AtomicDirectorySwap("demo", target)
        tmp = swap.prepare()
        _populate(tmp, "v2")

        real_rename = Path.rename

        def _boom(self: Path, target_path: str | Path) -> Path:
            if self == target:
                msg = "permission denied"
                raise OSError(msg)
            return real_rename(self, target_path)

        monkeypatch.setattr(Path, "rename", _boom)

        with pytest.raises(OSError, match="permission denied"):
            swap.commit(tmp)

        assert target.is_dir()  # the original occupant is untouched
        assert (target / "file.txt").read_text(encoding="utf-8") == "v1"
        assert list(target.parent.glob(".demo.*")) == []
