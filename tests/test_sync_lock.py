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
        """If Popen succeeds but PID write fails, sync is running — return launched.

        The lock is left in place on a persistent write failure: Popen already
        succeeded (the sync IS running), so removing the lock would let the
        next SessionStart see none and launch a DUPLICATE sync. A stale lock
        costs one skipped sync; a removed one risks two running at once.
        """
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
        assert lockfile.exists()

    def test_short_write_is_retried_until_the_full_pid_lands(
        self, tmp_path: Path
    ) -> None:
        """A partial os.write is retried with the remainder, never dropped.

        Regression test: an unchecked ``os.write`` return count would let a
        short write leave a truncated PID that ``is_held()`` still
        ``int()``-parses, possibly matching an unrelated live process and
        wedging background sync forever. The fix must not solve that by
        unlinking the lockfile either -- Popen already succeeded, so a
        removed lock would let a concurrent SessionStart launch a duplicate
        sync. The correct fix is a write-all retry loop.
        """
        lockfile = tmp_path / "sync.pid"
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        lock = SyncLock(path=lockfile)
        real_write = os.write

        def one_byte_at_a_time(fd: int, data: bytes) -> int:
            """Write only the first byte of *data*, simulating a short write."""
            return real_write(fd, data[:1])

        with (
            patch.object(_subprocess, "Popen", return_value=mock_proc),
            patch("os.write", side_effect=one_byte_at_a_time),
        ):
            assert lock.launch_background_sync() == "launched"
        assert lockfile.read_text() == "99999"


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


class TestSyncLockAcquire:
    def test_creates_lockfile_with_mode_0600(self, tmp_path: Path) -> None:
        """The lockfile is created user-read/write-only, no group/other access."""
        lockfile = tmp_path / "sync.pid"
        lock = SyncLock(path=lockfile)
        fd = lock.acquire()
        assert fd is not None
        os.close(fd)
        assert lockfile.stat().st_mode & 0o777 == 0o600


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
