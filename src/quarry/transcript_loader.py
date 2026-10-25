"""Read one transcript once, strictly: its stamp, and the conversation text."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, final

from quarry.loaded_transcript import LoadedTranscript
from quarry.transcript_bytes import TranscriptBytes
from quarry.transcript_reader import TranscriptReader

if TYPE_CHECKING:
    from pathlib import Path


@final
class TranscriptLoader:
    """Load a transcript from the one read of its file, so the stamp and text agree.

    A fault is raised, never read as an empty conversation: backfill remembers an
    empty transcript and never reads it again, so a read that failed must not
    look like one.  The text parsed is the text that read returned
    (:class:`~quarry.transcript_bytes.TranscriptBytes`): there is no second read
    that could disagree with the first.
    """

    __slots__ = ()

    @staticmethod
    def load(path: Path) -> LoadedTranscript:
        """Return the transcript at *path*.

        Raises:
            OSError: The file could not be opened or read (``ENOENT`` if it is gone).
            UnicodeDecodeError: The file is not UTF-8.
            NotARegularFileError: The path is a pipe or a device.
            TranscriptTooLargeError: The file is over the read limit.
        """
        read, raw = TranscriptBytes.read(path, datetime.now(UTC))
        reader = TranscriptReader.from_text(path, raw.decode("utf-8"))
        return LoadedTranscript(read, reader.text())
