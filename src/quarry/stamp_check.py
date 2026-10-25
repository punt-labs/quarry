"""Whether a file stamp can still be trusted: not racy, and still the file's own."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.failure_reason import FailureReason
from quarry.file_stamp import FileStamp

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

logger = logging.getLogger(__name__)


@final
class StampCheck:
    """Judge a stamp taken when a file was read.

    A stamp is *racy* when the file was modified within :attr:`RACY_NS` of the
    moment it was read: a write in the same clock tick leaves the stamp
    unchanged, so a verdict on such a read cannot be trusted to still hold.  It
    *holds for* a path while the file there is exactly the one it describes.
    """

    RACY_NS: ClassVar[int] = 2_000_000_000

    __slots__ = ("_stamp",)

    _stamp: FileStamp

    def __new__(cls, stamp: FileStamp) -> Self:
        self = super().__new__(cls)
        self._stamp = stamp
        return self

    def is_racy(self, read_at: datetime) -> bool:
        """Return whether the file changed too near *read_at* to be trusted."""
        read_ns = int(read_at.timestamp() * 1_000_000_000)
        return read_ns - self._stamp.mtime_ns < self.RACY_NS

    def holds_for(self, path: Path) -> bool:
        """Return whether *path* is right now exactly the file the stamp describes."""
        try:
            return self._stamp == FileStamp.of(path.stat())
        except (OSError, ValueError) as exc:  # gone, or not a path at all
            logger.debug("stamp: cannot stat a file: %s", FailureReason.os_name(exc))
            return False
