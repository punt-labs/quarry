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
    written unlocked, with one warning per cause, and the reaper then judges its
    leftovers by age alone.
    """

    MODE: ClassVar[int] = 0o600
    UNLOCKABLE: ClassVar[frozenset[int]] = frozenset(
        {errno.ENOLCK, errno.EOPNOTSUPP, errno.EBADF}
    )

    __slots__ = ("_directory", "_name", "_target")

    _directory: int
    _name: str
    _target: str

    def __new__(cls, directory: int, target: str) -> Self:
        self = super().__new__(cls)
        self._directory = directory
        self._target = target
        self._name = f".{target}.{secrets.token_hex(6)}.tmp"
        return self

    def commit(self, content: str) -> None:
        """Write *content* and rename it into place; leave nothing behind on failure."""
        fd = os.open(
            self._name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            self.MODE,
            dir_fd=self._directory,
        )
        try:
            self._write_locked(fd, content)
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

    def _write_locked(self, fd: int, content: str) -> None:
        """Write *content* through *fd* under its lock, then rename it into place."""
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8", newline="")
        except BaseException:
            os.close(fd)  # fdopen did not take it
            raise
        with handle:
            self._lock(handle.fileno())
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.rename(
                self._name,
                self._target,
                src_dir_fd=self._directory,
                dst_dir_fd=self._directory,
            )

    @classmethod
    def _lock(cls, fd: int) -> None:
        """Lock *fd* for the writer's life; warn once that this filesystem cannot."""
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in cls.UNLOCKABLE:
                raise
            cls._warn_unlocked(FailureReason.os_name(exc))

    @staticmethod
    @cache
    def _warn_unlocked(cause: str) -> None:
        """Warn, once per cause, that captures are written without a lock."""
        logger.warning(
            "capture: the filesystem cannot lock files (%s); writing unlocked, "
            "and leftovers are reaped by age alone",
            cause,
        )
