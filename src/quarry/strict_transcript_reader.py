"""A transcript reader that raises a read fault instead of reading it as empty."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.transcript_reader import TranscriptReader

if TYPE_CHECKING:
    from pathlib import Path


@final
class StrictTranscriptReader(TranscriptReader):
    """Read a transcript, but let a read fault out.

    :class:`TranscriptReader` degrades an unreadable file to an empty
    conversation, because a capture must never block compaction.  A caller that
    would act on "empty" (backfill remembers it, and never reads the file again)
    must tell the two apart, so this reads the file once, strictly, first.
    """

    __slots__ = ()

    def __new__(cls, path: Path) -> Self:
        path.read_text()  # raises OSError / UnicodeDecodeError; the result is unused
        return super().__new__(cls, path)
