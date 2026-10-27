"""The file the unindexable-transcript cache lives in."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Self, final

from quarry.atomic_file import AtomicFile
from quarry.failure_reason import FailureReason
from quarry.file_stamp import FileStamp
from quarry.unindexable_entries import StampedEntries

logger = logging.getLogger(__name__)

# A file this size holds hundreds of thousands of transcripts; anything larger is
# not ours, and parsing it would cost more than re-reading the transcripts.
_MAX_BYTES = 4 * 1024 * 1024


@final
class UnindexableFile:
    """Load and save the cache's JSON map of transcript path to file stamp.

    A file that is missing, oversized, unreadable or of the wrong shape loads as
    empty (with one warning naming the path and why), so the cost of losing it is
    a re-read, never a wrong skip.  A file that held anything it could not use is
    not a clean copy of what was loaded, so :meth:`holds` is false for every map
    until it is rewritten: the warning is not repeated by every run.  Saving is
    atomic, mode 0600, and reports a fault by name instead of raising: the cache
    is an optimization, so a run is never decided by whether it could be saved.
    """

    __slots__ = ("_on_disk", "_path")

    # None: the file on disk is not a clean copy of any map, so it is rewritten.
    _on_disk: dict[str, FileStamp] | None
    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        self._on_disk = None
        return self

    def holds(self, known: dict[str, FileStamp]) -> bool:
        """Return whether the file on disk is a clean copy of *known*."""
        return known == self._on_disk

    def load(self) -> dict[str, FileStamp]:
        """Return the saved stamps, or an empty map if there is no usable file."""
        self._on_disk = None
        try:
            if not self._path.is_file():
                self._on_disk = {}
                return {}
            if self._path.stat().st_size > _MAX_BYTES:
                logger.warning(
                    "backfill: %s is over %d bytes; ignoring", self._path, _MAX_BYTES
                )
                return {}
            raw = json.loads(AtomicFile(self._path).read())
        except (OSError, ValueError, RecursionError) as exc:
            logger.warning(
                "backfill: %s unreadable (%s); ignoring",
                self._path,
                type(exc).__name__,
            )
            return {}
        entries = StampedEntries(raw, self._path)
        if not entries.dirty:
            self._on_disk = entries.kept
        return entries.kept

    def write(self, known: dict[str, FileStamp]) -> str:
        """Save *known*; return the fault's name if that failed, else ``""``."""
        try:
            text = json.dumps(
                {p: s.as_json() for p, s in known.items()}, sort_keys=True
            )
            AtomicFile(self._path).replace(text, mode=0o600)
        except (OSError, ValueError, TypeError) as exc:
            fault = FailureReason.os_name(exc)
            logger.warning("backfill: could not save %s: %s", self._path, fault)
            return fault
        self._on_disk = dict(known)
        return ""
