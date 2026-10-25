"""A transcript as read: what was read, and the conversation text in it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from quarry.transcript_read import TranscriptRead


@final
@dataclass(frozen=True, slots=True)
class LoadedTranscript:
    """A transcript with the moment and stamp it was read at, and its text."""

    read: TranscriptRead
    text: str
