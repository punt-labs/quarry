"""The spool directory's lock: writers share it, the sweep holds it alone."""

from __future__ import annotations

import errno
import fcntl
import logging
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, ClassVar, Self, final

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

logger = logging.getLogger(__name__)


@final
class SpoolLock:
    """An ``flock`` on a file in the spool directory.

    Every write holds it shared and the startup sweep holds it exclusive, so a
    sweep, in this daemon or in another one on the same root, never meets a write
    in flight, and writes never meet a sweep.  Closing the descriptor releases
    the lock, so it is released on every path.

    A lock that is free is taken without a word.  One that is held is waited for
    after saying so, since the wait can last as long as the holder's disk stalls.
    A filesystem that cannot lock at all (some network mounts) is warned about
    once and then used unlocked: refusing to spool would lose the very records
    the spool exists to keep.
    """

    NAME: ClassVar[str] = ".lock"

    # What a filesystem that does not support ``flock`` answers with.
    _UNSUPPORTED: ClassVar[frozenset[int]] = frozenset(
        {errno.ENOLCK, errno.ENOTSUP, errno.EOPNOTSUPP}
    )
    _unsupported_warned: ClassVar[bool] = False

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, directory: Path) -> Self:
        self = super().__new__(cls)
        self._path = directory / cls.NAME
        return self

    @contextmanager
    def shared(self) -> Generator[None]:
        """Hold the lock shared (a write)."""
        with self._held(fcntl.LOCK_SH):
            yield

    @contextmanager
    def exclusive(self) -> Generator[None]:
        """Hold the lock alone (the sweep)."""
        with self._held(fcntl.LOCK_EX):
            yield

    @contextmanager
    def _held(self, mode: int) -> Generator[None]:
        """Take the lock in *mode*, waiting for it, and release it on exit."""
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self._acquire(fd, mode)
            yield
        finally:
            os.close(fd)

    def _acquire(self, fd: int, mode: int) -> None:
        """Lock *fd* in *mode*: try without blocking, say so, then block."""
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("spool: waiting for the spool lock (%s)", self._path.name)
            self._block(fd, mode)
        except OSError as exc:
            self._tolerate(exc)

    def _block(self, fd: int, mode: int) -> None:
        """Wait for the lock held by someone else."""
        try:
            fcntl.flock(fd, mode)
        except OSError as exc:
            self._tolerate(exc)

    @classmethod
    def _tolerate(cls, exc: OSError) -> None:
        """Go on unlocked if the filesystem cannot lock; raise anything else."""
        if exc.errno not in cls._UNSUPPORTED:
            raise
        if not cls._unsupported_warned:
            cls._unsupported_warned = True
            logger.warning(
                "spool: this filesystem does not support flock (%s); "
                "the spool is used without its lock",
                errno.errorcode.get(exc.errno or 0, "unknown"),
            )
