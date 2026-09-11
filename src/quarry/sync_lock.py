"""The single-flight guard around ``quarry sync``'s detached background launch."""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Self, final

logger = logging.getLogger(__name__)


@final
class SyncLock:
    """Own the sync PID lockfile: presence, staleness, atomic acquire, launch.

    One instance, one lockfile path — every method reads or mutates
    ``self._path``.  The constructor's optional *path* is a real capability
    a free-function lockfile-getter never had: tests inject a ``tmp_path``
    lockfile directly instead of patching a private module function.
    """

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path | None = None) -> Self:
        self = super().__new__(cls)
        self._path = path if path is not None else cls._default_path()
        return self

    @staticmethod
    def _default_path() -> Path:
        """Return the default lockfile path in a user-owned directory."""
        return Path.home() / ".punt-labs" / "quarry" / "sync.pid"

    @property
    def path(self) -> Path:
        """Return the lockfile path this lock guards."""
        return self._path

    def is_held(self) -> bool:
        """Check whether a quarry sync process is already running via PID file.

        Returns True if a live sync process exists, False otherwise.
        Stale PID files (process no longer running) are cleaned up.

        Handles signal-0 results correctly:
        - ProcessLookupError -> process is gone (stale)
        - PermissionError (EPERM) -> process exists, another user (running)
        - ValueError -> corrupt PID file (stale)
        - FileNotFoundError -> a concurrent is_held() reclaimed the file
          between the exists() check above and read_text() below (stale)
        """
        if not self._path.exists():
            return False
        try:
            pid = int(self._path.read_text().strip())
            if pid <= 0:
                raise ValueError("non-positive PID")
            os.kill(pid, 0)
            return True
        except PermissionError:
            # EPERM: process exists but we can't signal it — treat as running.
            return True
        except (FileNotFoundError, ValueError, ProcessLookupError):
            # Stale PID file — process is gone, PID is garbage, or a
            # concurrent caller already unlinked it out from under us.
            with contextlib.suppress(OSError):
                self._path.unlink()
            return False

    def acquire(self) -> int | None:
        """Atomically create the lock file and return the fd.

        Uses O_CREAT|O_EXCL to prevent TOCTOU races: if the file already
        exists, os.open raises FileExistsError and no lock is acquired.

        Returns the file descriptor on success, None if the lock is held
        or on any OS error.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return os.open(
                str(self._path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            return None
        except OSError as exc:
            logger.error("session-start: failed to create lock file: %s", exc)
            return None

    def launch_background_sync(self) -> str:
        """Fire-and-forget sync via detached subprocess.

        Uses ``sys.executable -m quarry`` to avoid PATH trust issues (the
        hook runs automatically on SessionStart with no user confirmation).
        Redirects all stdio to DEVNULL — especially stdin, to prevent the
        child from holding Claude Code's stdin pipe open after the parent
        exits.  The subprocess gets its own process group so it survives
        the hook process.

        Guards against concurrent syncs via this lock's atomic PID file.
        Uses O_CREAT|O_EXCL to prevent TOCTOU races between concurrent
        SessionStart hooks.

        Returns ``"launched"`` if the subprocess was started, ``"running"``
        if a sync is already in progress (or the lock is held), or
        ``"failed"`` if the launch itself errored.
        """
        # Fast path: if a sync is already running, skip without trying the lock.
        if self.is_held():
            logger.debug("session-start: sync already running, skipping")
            return "running"

        # Atomic lock acquisition — prevents TOCTOU races.
        fd = self.acquire()
        if fd is None:
            logger.debug("session-start: could not acquire sync lock, skipping")
            return "running"

        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "quarry", "sync"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            logger.error("session-start: failed to launch background sync: %s", exc)
            # Clean up the lock file since no sync is running.
            os.close(fd)
            with contextlib.suppress(OSError):
                self._path.unlink()
            return "failed"

        # Write the PID to the lock file (fd is already open).  A short write
        # (n < len(data)) would leave a truncated PID that is_held() still
        # int()-parses — possibly matching an unrelated live process and
        # wedging background sync forever — so an incomplete write is treated
        # the same as an OSError: warn and drop the corrupted lock file.
        try:
            data = str(proc.pid).encode()
            written = os.write(fd, data)
            if written != len(data):
                msg = f"short write ({written}/{len(data)} bytes)"
                raise OSError(msg)
        except OSError as exc:
            logger.warning(
                "session-start: sync launched but pidfile write failed: %s", exc
            )
            with contextlib.suppress(OSError):
                self._path.unlink()
        finally:
            os.close(fd)

        logger.info("session-start: background sync launched (pid=%d)", proc.pid)
        return "launched"
