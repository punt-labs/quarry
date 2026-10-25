"""The entries of a loaded unindexable-transcript cache that can be trusted."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Self, cast, final

from quarry.file_stamp import FileStamp

logger = logging.getLogger(__name__)


@final
class StampedEntries:
    """The entries of a saved cache that are a file stamp of an absolute path.

    A key must be a string, free of NUL, and absolute: anything else could never
    name a transcript the scan lists, and a NUL would make every later ``stat``
    of it raise.  A value must be a stamp.  What does not qualify is dropped, with
    one warning naming *source*, and the entries are then :attr:`dirty`: the cache
    file is rewritten clean, so the warning is not repeated by every run.
    """

    __slots__ = ("_dirty", "_kept")

    _dirty: bool
    _kept: dict[str, FileStamp]

    def __new__(cls, raw: object, source: Path) -> Self:
        self = super().__new__(cls)
        self._kept = {}
        self._dirty = False
        if not isinstance(raw, dict):
            logger.warning(
                "backfill: %s is not an object (%s); ignoring",
                source,
                type(raw).__name__,
            )
            self._dirty = True
            return self
        saved = cast("dict[object, object]", raw)
        for path, stamp in saved.items():
            try:
                self._kept[cls._key(path)] = FileStamp.from_json(stamp)
            except ValueError:
                continue
        dropped = len(saved) - len(self._kept)
        if dropped:
            self._dirty = True
            logger.warning(
                "backfill: %s: dropped %d entries that are not stamps of paths",
                source,
                dropped,
            )
        return self

    @property
    def kept(self) -> dict[str, FileStamp]:
        """Return the entries that qualified."""
        return self._kept

    @property
    def dirty(self) -> bool:
        """Return whether anything was dropped."""
        return self._dirty

    @staticmethod
    def prune(known: dict[str, FileStamp]) -> dict[str, FileStamp]:
        """Return *known* without the transcripts that can no longer be examined."""
        live: dict[str, FileStamp] = {}
        for path, stamp in known.items():
            try:
                Path(path).stat()
            except (OSError, ValueError):  # gone, or not a path at all
                continue
            live[path] = stamp
        return live

    @staticmethod
    def _key(path: object) -> str:
        """Return *path* if it is an absolute path, else raise ``ValueError``."""
        if isinstance(path, str) and "\0" not in path and Path(path).is_absolute():
            return path
        msg = "not an absolute path"
        raise ValueError(msg)
