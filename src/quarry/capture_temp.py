"""The temp file a capture is written through: locked, owner-only, renamed."""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import secrets
from functools import cache
from typing import ClassVar, Self, final

from quarry.failure_reason import FailureReason

logger = logging.getLogger(__name__)


@final
class CaptureTemp:
    """Write a capture to a temp file in a directory, then rename it over *target*.

    The temp file is created there (``O_EXCL``, ``O_NOFOLLOW``) mode 0600 from the
    first byte, locked for the writer's life (how a reaper tells a crashed
    writer's leftover from a live one's), flushed and synced, and renamed through
    the same directory descriptor, so nothing is written or replaced outside it.
    A failure at any step removes the temp file.

    A filesystem that cannot lock (:attr:`UNLOCKABLE`: NFS without a lock
    daemon, some FUSE mounts) is no reason to refuse the write: the file is
    written unlocked, with one warning per cause and captures directory, and the
    reaper then judges its leftovers by age alone.  :meth:`commit` says whether
    the write was locked, so the caller can count the ones that were not.  An
    ``EBADF`` is a bug in the descriptor, never a filesystem limit, so it raises.
    """

    MODE: ClassVar[int] = 0o600
    UNLOCKABLE: ClassVar[frozenset[int]] = frozenset({errno.ENOLCK, errno.EOPNOTSUPP})

    __slots__ = ("_directory", "_location", "_name", "_target")

    _directory: int
    _location: str
    _name: str
    _target: str

    def __new__(cls, directory: int, target: str, location: str) -> Self:
        self = super().__new__(cls)
        self._directory = directory
        self._location = location
        self._target = target
        self._name = f".{target}.{secrets.token_hex(6)}.tmp"
        return self

    def commit(self, content: str) -> bool:
        """Write *content* and rename it into place; return whether it was locked.

        Leaves nothing behind on failure.
        """
        fd = os.open(
            self._name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            self.MODE,
            dir_fd=self._directory,
        )
        try:
            return self._write_locked(fd, content)
        except BaseException:
            self._remove()
            raise

    def _remove(self) -> None:
        """Remove the temp file after a failure; a failure to is only diagnostic."""
        try:
            os.unlink(self._name, dir_fd=self._directory)
        except OSError as exc:
            logger.debug(
                "capture: could not remove temp file %s: %s",
                self._name,
                FailureReason.os_name(exc),
            )

    def _write_locked(self, fd: int, content: str) -> bool:
        """Write *content* through *fd* under its lock, then rename it into place.

        Returns whether the lock was taken.
        """
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8", newline="")
        except BaseException:
            os.close(fd)  # fdopen did not take it
            raise
        with handle:
            locked = self._lock(handle.fileno())
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.rename(
                self._name,
                self._target,
                src_dir_fd=self._directory,
                dst_dir_fd=self._directory,
            )
        return locked

    def _lock(self, fd: int) -> bool:
        """Lock *fd* for the writer's life; return False on a filesystem that cannot."""
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in self.UNLOCKABLE:
                raise
            self._warn_unlocked(FailureReason.os_name(exc), self._location)
            return False
        return True

    @staticmethod
    @cache
    def _warn_unlocked(cause: str, location: str) -> None:
        """Warn, once per cause and directory, that captures are written unlocked."""
        logger.warning(
            "capture: %s cannot lock files (%s); writing unlocked, "
            "and leftovers are reaped by age alone",
            location,
            cause,
        )
