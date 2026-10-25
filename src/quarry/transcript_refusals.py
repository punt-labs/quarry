"""Why a transcript is refused before it is read: what it is, and what it was."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from quarry.refusal import RefusalError

if TYPE_CHECKING:
    from quarry.transcript_read import TranscriptRead


class UnreadTranscriptError(RefusalError):
    """A transcript refused by what it is; the message is a fixed reason.

    :attr:`read` is the file as it stood when it was refused (its stamp), so a
    refusal that will not change can be remembered against that stamp.
    """

    _read: TranscriptRead

    def about(self, read: TranscriptRead) -> Self:
        """Attach the transcript's stamp and return the refusal, ready to raise."""
        self._read = read
        return self

    @property
    def read(self) -> TranscriptRead:
        """Return the file as it stood when it was refused."""
        return self._read


class TranscriptTooLargeError(UnreadTranscriptError):
    """The transcript is over the backfill read limit."""


class NotARegularFileError(UnreadTranscriptError):
    """The transcript is a pipe or a device, not a file."""
