"""The temp file a capture is written through: locked, owner-only, renamed."""

from __future__ import annotations

import contextlib
import fcntl
import os
import secrets
from typing import ClassVar, Self, final


@final
class CaptureTemp:
    """Write a capture to a temp file in a directory, then rename it over *target*.

    The temp file is created there (``O_EXCL``, ``O_NOFOLLOW``) mode 0600 from the
    first byte, locked for the writer's life (how a reaper tells a crashed
    writer's leftover from a live one's), flushed and synced, and renamed through
    the same directory descriptor, so nothing is written or replaced outside it.
    A failure at any step removes the temp file.
    """

    MODE: ClassVar[int] = 0o600

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
            with contextlib.suppress(OSError):
                os.unlink(self._name, dir_fd=self._directory)
            raise

    def _write_locked(self, fd: int, content: str) -> None:
        """Write *content* through *fd* under its lock, then rename it into place."""
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8", newline="")
        except BaseException:
            os.close(fd)  # fdopen did not take it
            raise
        with handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.rename(
                self._name,
                self._target,
                src_dir_fd=self._directory,
                dst_dir_fd=self._directory,
            )
