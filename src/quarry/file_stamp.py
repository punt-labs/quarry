"""A file's mtime and size: what it must still be for a cached verdict to hold."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, cast, final

if TYPE_CHECKING:
    import os


@final
@dataclass(frozen=True, slots=True)
class FileStamp:
    """A file as it was when it was read: its mtime in nanoseconds, size and identity.

    Any one can be unchanged by a change (a rewrite within the clock's
    granularity keeps the mtime; an edit in place keeps the size; a file moved
    over it keeps both), so a cached verdict is keyed on all of them: the mtime,
    the size, and the inode and device the file lives at.
    """

    mtime_ns: int
    size: int
    ino: int = 0
    dev: int = 0

    @classmethod
    def of(cls, st: os.stat_result) -> Self:
        """Return the stamp of the file *st* describes."""
        return cls(st.st_mtime_ns, st.st_size, st.st_ino, st.st_dev)

    @classmethod
    def from_json(cls, raw: object) -> Self:
        """Return the stamp :meth:`as_json` wrote, or raise ``ValueError``."""
        return cls(*cls._four_ints(raw))

    @staticmethod
    def _four_ints(raw: object) -> list[int]:
        """Return *raw* if it is a JSON array of four integers, else raise."""
        if isinstance(raw, list):
            values = cast("list[object]", raw)
            if len(values) == 4 and all(type(n) is int for n in values):
                return cast("list[int]", values)
        msg = "not a file stamp"
        raise ValueError(msg)

    def as_json(self) -> list[int]:
        """Return the stamp as the JSON array the cache file stores."""
        return [self.mtime_ns, self.size, self.ino, self.dev]
