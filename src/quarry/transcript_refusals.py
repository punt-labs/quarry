"""Why a transcript is refused before it is read: what it is."""

from __future__ import annotations

from quarry.refusal import RefusalError


class UnreadTranscriptError(RefusalError):
    """A transcript refused by what it is; the message is a fixed reason."""


class TranscriptTooLargeError(UnreadTranscriptError):
    """The transcript is over the backfill read limit."""


class NotARegularFileError(UnreadTranscriptError):
    """The transcript is a pipe, a device or a symlink, not a file."""
