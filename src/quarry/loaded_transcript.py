"""A transcript as read: what was read, and the conversation text in it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from quarry.transcript_read import TranscriptRead


@final
@dataclass(frozen=True, slots=True)
class LoadedTranscript:
    """A transcript with the moment and stamp it was read at, and its text.

    ``truncated`` counts the records or entries its read lost to a bound (the
    oldest dropped, or the newest entry cut to its tail): the text is then the
    newest part of the conversation.
    """

    read: TranscriptRead
    text: str
    truncated: int = 0
