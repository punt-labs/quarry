"""The spool directory's lock: writers share it, the sweep holds it alone."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, ClassVar, Self, final

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


@final
class SpoolLock:
    """An ``flock`` on a file in the spool directory.

    Every write holds it shared and the startup sweep holds it exclusive, so a
    sweep, in this daemon or in another one on the same root, never meets a write
    in flight, and writes never meet a sweep.  Closing the descriptor releases
    the lock, so it is released on every path.
    """

    NAME: ClassVar[str] = ".lock"

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
            fcntl.flock(fd, mode)
            yield
        finally:
            os.close(fd)
