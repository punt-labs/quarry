"""One capture file on disk: owner-only, never through a symlink, durable."""

from __future__ import annotations

import contextlib
import logging
import os
import time
from pathlib import Path
from typing import ClassVar, Self, final

from quarry.atomic_file import AtomicFile

logger = logging.getLogger(__name__)


@final
class CaptureFile:
    """Write a capture where only its owner can read it, and no further than its path.

    A capture holds a scrubbed conversation, so it is written mode 0600, from the
    first byte (the temp file is created that way, not chmod-ed after).  The
    captures directory travels with a repository, so a clone can carry a symlink
    where a capture will be written; the write is refused rather than followed to
    wherever it points.  After the rename the directory is synced, so a capture
    that was reported written survives a power loss.  A temp file a crashed write
    left behind is reaped once it is old enough that no live write can own it.
    """

    # An in-flight write holds its temp file for milliseconds; one this old was
    # left by a crash.
    STALE_TEMP_S: ClassVar[float] = 3600.0
    MODE: ClassVar[int] = 0o600

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    def write(self, content: str) -> bool:
        """Write *content* to the capture path atomically, owner-only.

        Return whether it landed: ``False`` (with the reason logged) when the
        path is a symlink, which is never written through.

        Raises:
            OSError: The write could not be made durable.
        """
        if self._path.is_symlink():
            logger.warning("capture: %s is a symlink; not written", self._path.name)
            return False
        self._reap_leftovers()
        AtomicFile(self._path).replace(content, mode=self.MODE)
        self._sync_directory()
        return True

    def _sync_directory(self) -> None:
        """Flush the directory entry the rename just wrote."""
        fd = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _reap_leftovers(self) -> None:
        """Remove the stale temp files of crashed capture writes in this directory.

        Best effort: a leftover that cannot be removed is left for the next write.
        """
        cutoff = time.time() - self.STALE_TEMP_S
        for leftover in self._path.parent.glob(".session-*.md.*.tmp"):
            with contextlib.suppress(OSError):
                if leftover.stat().st_mtime < cutoff:
                    leftover.unlink()
                    logger.info("capture: reaped a stale temp file %s", leftover.name)
