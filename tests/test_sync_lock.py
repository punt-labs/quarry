"""Tests for :class:`quarry.sync_lock.SyncLock`."""

from __future__ import annotations

import os
import subprocess as _subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from quarry.sync_lock import SyncLock


class TestSyncLockLaunchBackgroundSync:
    def test_returns_launched_on_success(self, tmp_path: Path) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        lockfile = tmp_path / "sync.pid"
        lock = SyncLock(path=lockfile)
        with patch.object(_subprocess, "Popen", return_value=mock_proc):
            assert lock.launch_background_sync() == "launched"
            assert lockfile.exists()
            assert lockfile.read_text() == "99999"

    def test_returns_failed_on_oserror(self, tmp_path: Path) -> None:
        lockfile = tmp_path / "sync.pid"
        lock = SyncLock(path=lockfile)
        with patch.object(_subprocess, "Popen", side_effect=OSError("No such file")):
            assert lock.launch_background_sync() == "failed"
            assert not lockfile.exists()  # Lock cleaned up on failure

    def test_returns_running_when_already_running(self, tmp_path: Path) -> None:
        lockfile = tmp_path / "sync.pid"
        lockfile.write_text(str(os.getpid()))  # Current process — definitely alive
        lock = SyncLock(path=lockfile)
        assert lock.launch_background_sync() == "running"

    def test_returns_running_when_lock_held(self, tmp_path: Path) -> None:
        """Atomic lock prevents TOCTOU race — second caller gets 'running'."""
        lockfile = tmp_path / "sync.pid"
        lockfile.write_text("12345")  # Pre-existing lock file, not a live PID
        lock = SyncLock(path=lockfile)
        with patch("os.kill", side_effect=ProcessLookupError):
            # is_held() finds no live process and unlinks the stale file, but
            # a concurrent acquirer recreated it before this caller's acquire()
            # runs — modeled here by pre-seeding the file back after unlink.
            assert lock.is_held() is False
        lockfile.write_text("12345")
        with patch("quarry.sync_lock.os.open", side_effect=FileExistsError):
            assert lock.launch_background_sync() == "running"

    def test_pidfile_write_failure_still_returns_launched(self, tmp_path: Path) -> None:
        """If Popen succeeds but PID write fails, sync is running — return launched."""
        lockfile = tmp_path / "sync.pid"
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        lock = SyncLock(path=lockfile)

        with (
            patch.object(_subprocess, "Popen", return_value=mock_proc),
            patch("os.write", side_effect=OSError("disk full")),
        ):
            # Sync launched despite write failure.
            assert lock.launch_background_sync() == "launched"
        # The corrupted (empty) lockfile is dropped, not left behind for a
        # future is_held() to misread.
        assert not lockfile.exists()

    def test_short_write_drops_corrupted_pidfile_but_still_launched(
        self, tmp_path: Path
    ) -> None:
        """A partial os.write leaves a truncated PID -- treat it as a failure.

        Regression test: an unchecked ``os.write`` return count would let a
        short write leave a truncated PID that ``is_held()`` still
        ``int()``-parses, possibly matching an unrelated live process and
        wedging background sync forever.
        """
        lockfile = tmp_path / "sync.pid"
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        lock = SyncLock(path=lockfile)

        with (
            patch.object(_subprocess, "Popen", return_value=mock_proc),
            patch("os.write", return_value=1),  # "99999" is 5 bytes; only 1 written
        ):
            assert lock.launch_background_sync() == "launched"
        # No truncated pidfile survives to misfire a later is_held().
        assert not lockfile.exists()


class TestSyncLockIsHeld:
    def test_no_pidfile_returns_false(self, tmp_path: Path) -> None:
        lock = SyncLock(path=tmp_path / "sync.pid")
        assert lock.is_held() is False

    def test_stale_pid_returns_false(self, tmp_path: Path) -> None:
        pidfile = tmp_path / "sync.pid"
        pidfile.write_text("999999999")  # PID that doesn't exist
        lock = SyncLock(path=pidfile)
        assert lock.is_held() is False
        assert not pidfile.exists()  # Stale file cleaned up

    def test_live_pid_returns_true(self, tmp_path: Path) -> None:
        pidfile = tmp_path / "sync.pid"
        pidfile.write_text(str(os.getpid()))  # Current process — definitely alive
        lock = SyncLock(path=pidfile)
        assert lock.is_held() is True

    def test_eperm_treated_as_running(self, tmp_path: Path) -> None:
        """PermissionError (EPERM) means process exists but not ours."""
        pidfile = tmp_path / "sync.pid"
        pidfile.write_text("1")  # PID 1 (init) — will get EPERM
        lock = SyncLock(path=pidfile)
        with patch("os.kill", side_effect=PermissionError("EPERM")):
            assert lock.is_held() is True
            assert pidfile.exists()  # Not cleaned up — process is alive

    def test_negative_pid_treated_as_stale(self, tmp_path: Path) -> None:
        pidfile = tmp_path / "sync.pid"
        pidfile.write_text("-1")
        lock = SyncLock(path=pidfile)
        assert lock.is_held() is False
        assert not pidfile.exists()

    def test_file_removed_between_exists_and_read_text_is_not_held(
        self, tmp_path: Path
    ) -> None:
        """A concurrent is_held() reclaiming the file mid-check must not raise.

        Regression test: exists() and read_text() are two separate syscalls;
        a second SessionStart hook that unlinks the stale lockfile in
        between must be treated as "lock is gone", not propagate a
        FileNotFoundError out of the hook.
        """
        pidfile = tmp_path / "sync.pid"
        pidfile.write_text("12345")
        lock = SyncLock(path=pidfile)
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            assert lock.is_held() is False


class TestSyncLockConstruction:
    def test_default_path_is_under_home_punt_labs_quarry(self) -> None:
        lock = SyncLock()
        assert lock.path == Path.home() / ".punt-labs" / "quarry" / "sync.pid"

    def test_explicit_path_is_honored(self, tmp_path: Path) -> None:
        custom = tmp_path / "custom.pid"
        lock = SyncLock(path=custom)
        assert lock.path == custom

    def test_two_instances_with_different_paths_are_independent(
        self, tmp_path: Path
    ) -> None:
        """Distinct lockfile paths guard distinct sync flows independently."""
        a = SyncLock(path=tmp_path / "a.pid")
        b = SyncLock(path=tmp_path / "b.pid")
        (tmp_path / "a.pid").write_text(str(os.getpid()))
        assert a.is_held() is True
        assert b.is_held() is False
