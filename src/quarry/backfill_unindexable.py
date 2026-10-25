"""The transcripts backfill has found to hold nothing to index, kept across runs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.backfill_unindexable_file import UnindexableFile
from quarry.os_fault import OsFault

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.transcript_read import TranscriptRead

logger = logging.getLogger(__name__)


@final
class UnindexableTranscripts:
    """Remember a transcript that chunked to nothing, as of the mtime it had then.

    A conversation of tool calls only has no text to store, so it is never
    stored and never "current": every run would read it again and find the same
    nothing.  The cache keeps its mtime, beside the registry, so a later run skips
    it until the transcript changes.  A transcript is recorded only if its mtime
    is still the one it had before it was read, so text appended mid-run is never
    skipped as empty.
    """

    __slots__ = ("_file", "_known", "_saved")

    _file: UnindexableFile
    _known: dict[str, int]
    _saved: dict[str, int]

    def __new__(cls, store: Path) -> Self:
        self = super().__new__(cls)
        self._file = UnindexableFile(store)
        self._saved = self._file.load()
        self._known = dict(self._saved)
        return self

    def knows(self, transcript: Path) -> bool:
        """Return whether *transcript* is known to be unindexable and is unchanged."""
        try:
            mtime = transcript.stat().st_mtime_ns
        except OSError:
            return False
        return self._known.get(str(transcript)) == mtime

    def record(self, read: TranscriptRead) -> None:
        """Note that *read*'s transcript held nothing to index, if it is unchanged."""
        try:
            current = read.transcript.stat().st_mtime_ns
        except OSError as exc:
            logger.debug("backfill: cannot record a transcript: %s", OsFault.named(exc))
            return
        if current == read.mtime_ns:
            self._known[str(read.transcript)] = read.mtime_ns

    def save(self) -> str:
        """Write what was learned, pruned; return the fault's name if that failed."""
        live = self._file.prune(self._known)
        if live == self._saved:
            return ""
        fault = self._file.write(live)
        if not fault:
            self._known, self._saved = live, dict(live)
        return fault
