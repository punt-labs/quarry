"""Why a transcript is refused before it is read: what it is, and what it was."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from quarry.refusal import RefusalError

if TYPE_CHECKING:
    from quarry.transcript_read import TranscriptRead


@dataclass(eq=False)
class UnreadTranscriptError(RefusalError):
    """A transcript refused by what it is; the message is a fixed reason.

    :attr:`read` is the file as it stood when it was refused (its stamp).
    """

    message: str
    read: TranscriptRead

    def __str__(self) -> str:
        """Return the reason alone."""
        return self.message


class TranscriptTooLargeError(UnreadTranscriptError):
    """The transcript is over the backfill read limit."""


class NotARegularFileError(UnreadTranscriptError):
    """The transcript is a pipe, a device or a symlink, not a file."""
