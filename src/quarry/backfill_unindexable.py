"""The transcripts backfill has found to hold nothing to index, kept across runs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.backfill_unindexable_file import UnindexableFile
from quarry.unindexable_entries import StampedEntries

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.file_stamp import FileStamp
    from quarry.transcript_read import TranscriptRead


@final
class UnindexableTranscripts:
    """Remember a transcript that chunked to nothing, as of the stamp it had then.

    A conversation of tool calls only has no text to store, so it is never
    stored and never "current": every run would read it again and find the same
    nothing.  The cache keeps its mtime and size, beside the registry, so a
    later run skips it until the transcript changes.  A transcript is recorded
    only if it is exactly as it was when read and was not modified within the
    racy window before that read, so text appended mid-run, or in the same clock
    tick, is never skipped as empty.
    """

    __slots__ = ("_file", "_known")

    _file: UnindexableFile
    _known: dict[str, FileStamp]

    def __new__(cls, store: Path) -> Self:
        self = super().__new__(cls)
        self._file = UnindexableFile(store)
        self._known = dict(self._file.load())
        return self

    def knows(self, transcript: Path) -> bool:
        """Return whether *transcript* is known to be unindexable and is unchanged."""
        known = self._known.get(str(transcript))
        return known is not None and known.holds_for(transcript)

    def record(self, read: TranscriptRead) -> None:
        """Note that *read*'s transcript held nothing to index, if that will hold."""
        if read.stamp.is_racy(read.snapshot) or not read.stamp.holds_for(
            read.transcript
        ):
            return  # modified too near the read, or since: it may not be empty now
        self._known[str(read.transcript)] = read.stamp

    def save(self) -> str:
        """Write what was learned, pruned; return the fault's name if that failed."""
        live = StampedEntries.prune(self._known)
        if self._file.holds(live):
            return ""
        fault = self._file.write(live)
        if not fault:
            self._known = live
        return fault
