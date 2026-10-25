"""Record a process id in an open lockfile, for a human to read."""

from __future__ import annotations

import errno
import os
from typing import Self, final


@final
class LockPidFile:
    """The pid written into a lockfile that is held through an open descriptor.

    The pid is for observability only: the flock, not this content, is the lock.
    A short write is retried until the whole pid lands, so a later reader never
    sees a truncated or stale value; a write that makes no progress is an error,
    never a spin.
    """

    __slots__ = ("_fd",)

    _fd: int

    def __new__(cls, fd: int) -> Self:
        self = super().__new__(cls)
        self._fd = fd
        return self

    def record(self, pid: int) -> None:
        """Replace the file's content with *pid*.

        Raises:
            OSError: The file cannot be truncated or written, or a write makes no
                progress.  A failed write may leave part of the pid on disk.
        """
        os.ftruncate(self._fd, 0)
        data = str(pid).encode()
        written = 0
        while written < len(data):
            # An explicit offset: the descriptor may be shared, and its position
            # is not ours to assume.
            count = os.pwrite(self._fd, data[written:], written)
            if count == 0:
                raise OSError(errno.EIO, "short write")
            written += count
