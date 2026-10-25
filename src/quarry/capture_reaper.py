"""Remove the temp files crashed capture writes left in a captures directory."""

from __future__ import annotations

import errno
import fcntl
import fnmatch
import logging
import os
import stat
import time
from typing import ClassVar, Self, final

from quarry.capture_directory import CaptureDirectory, CaptureSymlinkError
from quarry.failure_reason import FailureReason

logger = logging.getLogger(__name__)


@final
class CaptureReaper:
    """Reap a captures directory's leftovers: temp files whose writer is gone.

    A live writer holds an ``flock`` on its temp file for as long as it writes,
    so a leftover is reaped only if it is old enough to have outlived any write
    and its lock can be taken.  Reaping happens once per backfill run for each
    project, never per write.  Whatever cannot be removed is reported once, with
    the errno of the first failure.
    """

    PATTERN: ClassVar[str] = ".session-*.md.*.tmp"
    # An in-flight write holds its temp file for milliseconds, locked from the
    # moment it exists: ten minutes is long past any live write.
    STALE_AFTER_S: ClassVar[float] = 600.0

    __slots__ = ("_directory",)

    _directory: CaptureDirectory

    def __new__(cls, directory: CaptureDirectory) -> Self:
        self = super().__new__(cls)
        self._directory = directory
        return self

    def reap(self) -> int:
        """Remove the stale leftovers; return how many were removed."""
        try:
            fd = self._directory.open(create=False)
        except (FileNotFoundError, CaptureSymlinkError):
            return 0
        reaped, failures, first = 0, 0, ""
        try:
            for entry in os.scandir(fd):
                if not fnmatch.fnmatch(entry.name, self.PATTERN):
                    continue
                try:
                    reaped += self._reap_one(fd, entry.name)
                except OSError as exc:
                    failures += 1
                    first = first or FailureReason.os_name(exc)
        finally:
            os.close(fd)
        if failures:
            logger.warning(
                "capture: could not reap %d leftover temp file(s): %s", failures, first
            )
        return reaped

    def _reap_one(self, directory: int, name: str) -> int:
        """Remove *name* if its writer is gone; return 1 if it was removed."""
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            return 0
        if time.time() - info.st_mtime < self.STALE_AFTER_S:
            return 0
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EWOULDBLOCK, errno.EAGAIN}:
                return 0  # a live writer holds it
            raise
        finally:
            os.close(fd)
        os.unlink(name, dir_fd=directory)
        logger.info("capture: reaped a leftover temp file %s", name)
        return 1
