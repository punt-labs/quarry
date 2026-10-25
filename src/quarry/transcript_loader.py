"""Read one transcript once, strictly: its stamp, and the conversation text."""

from __future__ import annotations

import errno
import os
import stat
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar, final

from quarry.file_stamp import FileStamp
from quarry.loaded_transcript import LoadedTranscript
from quarry.transcript_read import TranscriptRead
from quarry.transcript_reader import TranscriptReader
from quarry.transcript_refusals import NotARegularFileError, TranscriptTooLargeError

if TYPE_CHECKING:
    from pathlib import Path


@final
class TranscriptLoader:
    """Load a transcript from the one read of its file, so the stamp and text agree.

    A fault is raised, never read as an empty conversation: backfill remembers an
    empty transcript and never reads it again, so a read that failed must not
    look like one.  The text parsed is the text that one read returned: there is
    no second read that could disagree with the first.

    The file is opened ``O_NONBLOCK`` so a pipe can never hold the scan thread,
    and ``O_NOFOLLOW`` so a symlink is refused rather than followed to whatever
    it points at.  It is refused unless it is a regular file under
    :attr:`MAX_BYTES`, checked on ``st_size`` before anything is read, and again
    on what was read.  The cap is 32 MiB: the text is held as bytes and as a
    string, and :class:`~quarry.transcript_reader.TranscriptReader` keeps at most
    its newest records, which peaked at 201 MiB beyond the text on the worst of
    the pathological inputs measured, so a transcript at the cap costs about
    270 MiB of the daemon's memory for the length of its read.  A larger one is
    refused rather than truncated, so it is counted and named, not silently
    half-stored.  The stamp comes from the open file.
    """

    MAX_BYTES: ClassVar[int] = 32 * 1024 * 1024

    __slots__ = ()

    @classmethod
    def load(cls, path: Path) -> LoadedTranscript:
        """Return the transcript at *path*.

        Raises:
            OSError: The file could not be opened or read (``ENOENT`` if it is gone).
            UnicodeDecodeError: The file is not UTF-8.
            NotARegularFileError: The path is a pipe, a device or a symlink.
            TranscriptTooLargeError: The file is over the read limit.
        """
        read, raw = cls._read(path, datetime.now(UTC))
        reader = TranscriptReader.from_text(path, raw.decode("utf-8"))
        return LoadedTranscript(read, reader.text())

    @classmethod
    def _read(cls, path: Path, snapshot: datetime) -> tuple[TranscriptRead, bytes]:
        """Return what was read of *path* at *snapshot*, and its bytes."""

        def opener(name: str, flags: int) -> int:
            """Open without waiting on a pipe and without following a symlink."""
            try:
                return os.open(name, flags | os.O_NONBLOCK | os.O_NOFOLLOW)
            except OSError as exc:
                if exc.errno == errno.ELOOP:  # O_NOFOLLOW refused a symlink
                    stamp = FileStamp.of(os.lstat(name))
                    raise cls._not_a_file(
                        TranscriptRead(path, stamp, snapshot)
                    ) from exc
                raise

        with open(path, "rb", opener=opener) as handle:
            info = os.fstat(handle.fileno())
            read = TranscriptRead(path, FileStamp.of(info), snapshot)
            if not stat.S_ISREG(info.st_mode):
                raise cls._not_a_file(read)
            if info.st_size > cls.MAX_BYTES:
                raise cls._too_large(read)
            raw = handle.read(cls.MAX_BYTES + 1)
        if len(raw) > cls.MAX_BYTES:  # it grew while it was read
            raise cls._too_large(read)
        return read, raw

    @classmethod
    def _too_large(cls, read: TranscriptRead) -> TranscriptTooLargeError:
        """Return the refusal of *read*, naming the limit."""
        msg = f"transcript is over the {cls.MAX_BYTES >> 20} MiB backfill limit"
        return TranscriptTooLargeError(msg, read)

    @staticmethod
    def _not_a_file(read: TranscriptRead) -> NotARegularFileError:
        """Return the refusal of *read*: a pipe, a device or a symlink."""
        msg = "transcript is not a regular file"
        return NotARegularFileError(msg, read)
