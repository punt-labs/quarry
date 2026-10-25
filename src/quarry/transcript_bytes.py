"""The bytes of one transcript, read once and checked before they are trusted."""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING, ClassVar, final

from quarry.file_stamp import FileStamp
from quarry.transcript_read import TranscriptRead
from quarry.transcript_refusals import NotARegularFileError, TranscriptTooLargeError

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path


@final
class TranscriptBytes:
    """Read a transcript from one open file, refusing what is not a transcript.

    The file is opened ``O_NONBLOCK`` so a pipe can never hold the scan thread,
    and refused unless it is a regular file under :attr:`MAX_BYTES` (checked on
    ``st_size`` before anything is read, and again on what was read).  The cap is
    32 MiB: the text is held as bytes, as a string, as split lines and as parsed
    records at once, about eight times the file at worst, so a transcript at the
    cap costs about 256 MiB of the daemon's memory for the length of its read.
    A larger one is refused rather than truncated, so it is counted and named,
    not silently half-stored.  The stamp comes from the open file.
    """

    MAX_BYTES: ClassVar[int] = 32 * 1024 * 1024

    __slots__ = ()

    @classmethod
    def read(cls, path: Path, snapshot: datetime) -> tuple[TranscriptRead, bytes]:
        """Return what was read of *path* at *snapshot*, and its bytes.

        Raises:
            OSError: The file could not be opened or read (``ENOENT`` if it is gone).
            NotARegularFileError: The path is a pipe or a device.
            TranscriptTooLargeError: The file is over the read limit.
        """
        with open(path, "rb", opener=cls._open_nonblocking) as handle:
            info = os.fstat(handle.fileno())
            read = TranscriptRead(path, FileStamp.of(info), snapshot)
            if not stat.S_ISREG(info.st_mode):
                msg = "transcript is not a regular file"
                raise NotARegularFileError(msg).about(read)
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
        return TranscriptTooLargeError(msg).about(read)

    @staticmethod
    def _open_nonblocking(path: str, flags: int) -> int:
        """Open *path* so that opening a pipe returns at once instead of waiting."""
        return os.open(path, flags | os.O_NONBLOCK)
