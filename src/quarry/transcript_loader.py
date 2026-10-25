"""Read one transcript once, strictly: its stamp, and the conversation text."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar, final

from quarry.file_stamp import FileStamp
from quarry.refusal import RefusalError
from quarry.transcript_read import TranscriptRead
from quarry.transcript_reader import TranscriptReader

if TYPE_CHECKING:
    from pathlib import Path


class TranscriptTooLargeError(RefusalError):
    """The transcript is over the backfill read limit."""


@final
@dataclass(frozen=True, slots=True)
class LoadedTranscript:
    """A transcript as read: what was read, and the conversation text in it."""

    read: TranscriptRead
    text: str


@final
class TranscriptLoader:
    """Read a transcript from one open file, so the stamp, the text and the parse agree.

    A fault is raised, never read as an empty conversation: backfill remembers an
    empty transcript and never reads it again, so a read that failed must not
    look like one.  The stamp comes from the open file, and the text parsed is
    the text that read returned: there is no second read that could disagree.
    A transcript past :attr:`MAX_BYTES` (a conversation of that size is not a
    transcript but a log) is refused rather than truncated, so it is counted and
    named instead of silently half-stored.
    """

    MAX_BYTES: ClassVar[int] = 64 * 1024 * 1024

    __slots__ = ()

    @classmethod
    def load(cls, path: Path) -> LoadedTranscript:
        """Return the transcript at *path*.

        Raises:
            OSError: The file could not be opened or read (``ENOENT`` if it is gone).
            UnicodeDecodeError: The file is not UTF-8.
            TranscriptTooLargeError: The file is over the read limit.
        """
        snapshot = datetime.now(UTC)
        with path.open("rb") as handle:
            stamp = FileStamp.of(os.fstat(handle.fileno()))
            raw = handle.read(cls.MAX_BYTES + 1)
        if len(raw) > cls.MAX_BYTES:
            msg = "transcript is over the 64 MiB backfill limit"
            raise TranscriptTooLargeError(msg)
        reader = TranscriptReader.from_text(path, raw.decode("utf-8"))
        return LoadedTranscript(TranscriptRead(path, stamp, snapshot), reader.text())
