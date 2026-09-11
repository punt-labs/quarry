"""Tests for :class:`quarry.sync_lock.SyncLock`."""

from __future__ import annotations

import fcntl
import os
import subprocess as _subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from quarry.sync_lock import SyncLock


class TestSyncLockLaunchBackgroundSync:
    def test_returns_launched_on_success(self, tmp_path: Path) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 99999
        lockfile = tmp_path / "sync.pid"
        lock = SyncLock(path=lockfile)
        with patch.object(_subprocess, "Popen", return_value=mock_proc) as popen:
            assert lock.launch_background_sync() == "launched"
            assert lockfile.exists()
            assert lockfile.read_text() == "99999"
        # The lock fd was passed to the child so its flock survives exec.
        pass_fds = popen.call_args.kwargs["pass_fds"]
        assert len(pass_fds) == 1
        assert isinstance(pass_fds[0], int)

    def test_returns_failed_on_oserror(self, tmp_path: Path) -> None:
        lockfile = tmp_path / "sync.pid"
        lock = SyncLock(path=lockfile)
        with patch.object(_subprocess, "Popen", side_effect=OSError("No such file")):
            assert lock.launch_background_sync() == "failed"
        # The scaffold file is never deleted -- only its (never-held) flock
        # was released.
        assert lockfile.exists()

    def test_set_inheritable_failure_releases_lock_and_returns_failed(
        self, tmp_path: Path
    ) -> None:
        """An OSError from set_inheritable must not escape or leak the fd.

        Regression test: os.set_inheritable(fd, True) ran after acquire()
        returned the held fd but outside the try/except that closes it on
        failure. An OSError there would escape launch_background_sync(),
        breaking its "launched"/"running"/"failed" fail-open contract, AND
        leak the held lock fd -- the flock would stay held for the rest of
        the hook process, blocking every future sync.
        """
        lockfile = tmp_path / "sync.pid"
        lock = SyncLock(path=lockfile)
        with (
            patch.object(_subprocess, "Popen") as popen,
            patch("os.set_inheritable", side_effect=OSError("EBADF")),
        ):
            assert lock.launch_background_sync() == "failed"
        popen.assert_not_called()
        # The fd was closed (not leaked) -- a fresh acquire on the same path
        # must succeed, proving the lock is not stuck.
        fd = lock.acquire()
        assert fd is not None
        os.close(fd)

    def test_returns_running_when_already_running(self, tmp_path: Path) -> None:
        """Another instance genuinely holding the flock blocks a new launch."""
        lockfile = tmp_path / "sync.pid"
        holder = SyncLock(path=lockfile)
        held_fd = holder.acquire()
        assert held_fd is not None
        try:
            lock = SyncLock(path=lockfile)
            assert lock.launch_background_sync() == "running"
        finally:
            os.close(held_fd)

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
        short write leave a truncated PID observable to a human inspecting
        the file. The fix must not solve that by unlinking the lockfile
        either -- Popen already succeeded, so a removed lock would let a
        concurrent SessionStart launch a duplicate sync. The correct fix is
        a write-all retry loop.
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
        """Probing creates the shared lockfile scaffold but never holds it."""
        lockfile = tmp_path / "sync.pid"
        lock = SyncLock(path=lockfile)
        assert lock.is_held() is False
        assert lockfile.exists()

    def test_unheld_lockfile_with_stale_content_is_not_held(
        self, tmp_path: Path
    ) -> None:
        """Leftover text content never gates the lock -- only flock does.

        A pre-existing pidfile from a crashed prior run (no one currently
        holds its flock, since the kernel released it when that process
        died) must be freely acquirable, and its content is never inspected
        or removed by this probe.
        """
        pidfile = tmp_path / "sync.pid"
        pidfile.write_text("999999999")
        lock = SyncLock(path=pidfile)
        assert lock.is_held() is False
        assert pidfile.exists()
        assert pidfile.read_text() == "999999999"

    def test_returns_true_while_another_instance_holds_the_lock(
        self, tmp_path: Path
    ) -> None:
        pidfile = tmp_path / "sync.pid"
        holder = SyncLock(path=pidfile)
        held_fd = holder.acquire()
        assert held_fd is not None
        try:
            probe = SyncLock(path=pidfile)
            assert probe.is_held() is True
        finally:
            os.close(held_fd)

    def test_transient_unlock_error_fails_open_not_raise(self, tmp_path: Path) -> None:
        """A LOCK_UN OSError during the probe must not escape is_held().

        Regression test: the explicit best-effort release after a successful
        probe-acquire was unguarded -- a transient OSError from LOCK_UN would
        propagate out of is_held() and make SessionStart raise, instead of
        the intended fail-open (return False). The finally-close already
        releases the flock regardless, so the explicit unlock is redundant
        housekeeping and must never be allowed to raise.
        """
        pidfile = tmp_path / "sync.pid"
        lock = SyncLock(path=pidfile)
        real_flock = fcntl.flock

        def fail_only_on_unlock(fd: int, operation: int) -> None:
            if operation == fcntl.LOCK_UN:
                raise OSError("transient unlock failure")
            real_flock(fd, operation)

        with patch("fcntl.flock", side_effect=fail_only_on_unlock):
            assert lock.is_held() is False  # fails open, never raises
        # The finally-close released the flock regardless of the failed
        # explicit unlock -- a fresh acquire on the same path must succeed.
        fd = lock.acquire()
        assert fd is not None
        os.close(fd)


class TestSyncLockAcquire:
    def test_creates_lockfile_with_mode_0600(self, tmp_path: Path) -> None:
        """The lockfile is created user-read/write-only, no group/other access."""
        lockfile = tmp_path / "sync.pid"
        lock = SyncLock(path=lockfile)
        fd = lock.acquire()
        assert fd is not None
        os.close(fd)
        assert lockfile.stat().st_mode & 0o777 == 0o600

    def test_second_acquire_on_a_held_lock_returns_none(self, tmp_path: Path) -> None:
        lockfile = tmp_path / "sync.pid"
        first = SyncLock(path=lockfile)
        fd = first.acquire()
        assert fd is not None
        try:
            second = SyncLock(path=lockfile)
            assert second.acquire() is None
        finally:
            os.close(fd)

    def test_acquire_succeeds_again_once_the_holder_releases(
        self, tmp_path: Path
    ) -> None:
        lockfile = tmp_path / "sync.pid"
        first = SyncLock(path=lockfile)
        fd = first.acquire()
        assert fd is not None
        os.close(fd)  # release
        second = SyncLock(path=lockfile)
        fd2 = second.acquire()
        assert fd2 is not None
        os.close(fd2)

    def test_refuses_a_symlinked_lock_path(self, tmp_path: Path) -> None:
        """O_NOFOLLOW rejects a symlink planted at the fixed lock path.

        Regression test for CWE-59 (symlink following): without
        ``O_NOFOLLOW``, opening a symlinked ``sync.pid`` would follow it, and
        the subsequent ``ftruncate``/``write`` would clobber whatever the
        symlink points at instead of the lockfile itself.
        """
        target = tmp_path / "target.txt"
        target.write_text("do not touch")
        lockfile = tmp_path / "sync.pid"
        lockfile.symlink_to(target)

        lock = SyncLock(path=lockfile)
        assert lock.acquire() is None
        # The symlink target was never opened, let alone truncated/written.
        assert target.read_text() == "do not touch"

    def test_is_held_does_not_raise_on_a_symlinked_lock_path(
        self, tmp_path: Path
    ) -> None:
        """is_held()'s probe fails open (False); the authoritative acquire()
        still refuses the symlink, so launch_background_sync() ends up
        "running" either way -- never a raised exception, never a follow.
        """
        target = tmp_path / "target.txt"
        target.write_text("do not touch")
        lockfile = tmp_path / "sync.pid"
        lockfile.symlink_to(target)

        lock = SyncLock(path=lockfile)
        assert lock.is_held() is False
        assert lock.acquire() is None
        assert target.read_text() == "do not touch"


class TestSyncLockSingleFlight:
    """Regression coverage for the reclaim race Copilot found in PR #518.

    The old PID/``unlink``-based design let two callers independently decide
    the same stale PID was reclaimable; each unlinked, each recreated the
    lock via ``O_CREAT|O_EXCL``, and the second caller's later unlink could
    delete the first caller's freshly created lock, letting both launch.
    ``flock`` replaces that whole "decide staleness, then act on it" protocol
    with a single kernel-arbitrated decision, so there is no window for a
    second caller to interleave with.
    """

    def test_two_concurrent_racers_yield_exactly_one_winner(
        self, tmp_path: Path
    ) -> None:
        """Two SyncLock instances racing on a pre-existing stale pidfile.

        Both threads are released by the same barrier so their ``is_held()``
        + ``acquire()`` sequence (the exact sequence ``launch_background_sync``
        uses) races for real via the kernel, not via mocked interleaving.
        Neither fd is closed until both threads have reported a result, so
        the outcome depends only on the kernel's single-winner arbitration,
        never on which thread happens to finish first.
        """
        lockfile = tmp_path / "sync.pid"
        # Pre-existing content from a crashed prior run -- a "stale PID"
        # under the old design.  flock arbitrates on the FILE, not this
        # text, so it is irrelevant to the race and must survive untouched.
        lockfile.write_text("999999999")
        lock_a = SyncLock(path=lockfile)
        lock_b = SyncLock(path=lockfile)
        barrier = threading.Barrier(2)
        results: dict[str, int | None] = {}
        results_lock = threading.Lock()

        def race(name: str, lock: SyncLock) -> None:
            barrier.wait()
            outcome = None if lock.is_held() else lock.acquire()
            with results_lock:
                results[name] = outcome

        threads = [
            threading.Thread(target=race, args=("a", lock_a)),
            threading.Thread(target=race, args=("b", lock_b)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [fd for fd in results.values() if fd is not None]
        try:
            assert len(winners) == 1, (
                f"expected exactly one winner, got {len(winners)}: {results}"
            )
        finally:
            for fd in winners:
                os.close(fd)
        # The winner's lock (the shared scaffold file itself) was never
        # deleted by the loser -- this design never deletes it at all.
        assert lockfile.exists()

    def test_flock_survives_exec_and_blocks_concurrent_acquire(
        self, tmp_path: Path
    ) -> None:
        """The child inherits the SAME open file description via pass_fds.

        Launches a REAL subprocess (not mocked) that just sleeps briefly,
        passing it the acquired lock fd, and confirms a second acquire()
        attempt on the same path is denied WHILE the child is alive, then
        succeeds once the child has exited -- proving the flock persists
        across execve() rather than evaporating when this process closes
        its own copy of the fd.
        """
        lockfile = tmp_path / "sync.pid"
        lock = SyncLock(path=lockfile)
        fd = lock.acquire()
        assert fd is not None
        inheritable = True
        os.set_inheritable(fd, inheritable)
        proc = _subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.4)"],
            pass_fds=(fd,),
        )
        os.close(fd)  # our copy only -- the child's copy keeps it locked
        try:
            other = SyncLock(path=lockfile)
            assert other.acquire() is None
        finally:
            proc.wait(timeout=5)
        # The child has exited; the kernel has released its copy of the lock.
        fd2 = other.acquire()
        assert fd2 is not None
        os.close(fd2)


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
        fd = a.acquire()
        assert fd is not None
        try:
            assert a.is_held() is True
            assert b.is_held() is False
        finally:
            os.close(fd)
