"""A file's mtime and size: what it must still be for a cached verdict to hold."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self, cast, final

from quarry.failure_reason import FailureReason

if TYPE_CHECKING:
    import os
    from datetime import datetime
    from pathlib import Path

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class FileStamp:
    """A file as it was when it was read: its mtime in nanoseconds, and its size.

    Either alone can be unchanged by a write (a rewrite within the clock's
    granularity keeps the mtime; an edit in place keeps the size), so a cached
    verdict is keyed on both.  A stamp is *racy* when the file was modified
    within :attr:`RACY_NS` of the moment it was read: a write in the same clock
    tick leaves the stamp unchanged, so a verdict on such a read cannot be
    trusted to still hold.
    """

    RACY_NS: ClassVar[int] = 2_000_000_000

    mtime_ns: int
    size: int

    @classmethod
    def of(cls, st: os.stat_result) -> Self:
        """Return the stamp of the file *st* describes."""
        return cls(st.st_mtime_ns, st.st_size)

    @classmethod
    def from_json(cls, raw: object) -> Self:
        """Return the stamp :meth:`as_json` wrote, or raise ``ValueError``."""
        if isinstance(raw, list) and len(cast("list[object]", raw)) == 2:
            mtime_ns, size = cast("list[object]", raw)
            if type(mtime_ns) is int and type(size) is int:
                return cls(mtime_ns, size)
        msg = "not a file stamp"
        raise ValueError(msg)

    def as_json(self) -> list[int]:
        """Return the stamp as the JSON array the cache file stores."""
        return [self.mtime_ns, self.size]

    def is_racy(self, read_at: datetime) -> bool:
        """Return whether the file changed too near *read_at* to be trusted."""
        read_ns = int(read_at.timestamp() * 1_000_000_000)
        return read_ns - self.mtime_ns < self.RACY_NS

    def holds_for(self, path: Path) -> bool:
        """Return whether *path* is right now exactly the file this stamp describes."""
        try:
            return self == self.of(path.stat())
        except (OSError, ValueError) as exc:  # gone, or not a path at all
            logger.debug("stamp: cannot stat a file: %s", FailureReason.os_name(exc))
            return False
