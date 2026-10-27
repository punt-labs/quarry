"""What a session transcript was when backfill read it (#556)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.display_name import DisplayName
from quarry.transcript import Transcript

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class TranscriptSnapshot:
    """A transcript, and its ``"<size>:<mtime_ns>"`` version when it was read.

    ``version`` is ``""`` when the transcript could not be examined.
    """

    path: Path
    version: str

    @classmethod
    def taken(cls, transcript: Path) -> Self:
        """Snapshot *transcript* as it is now; take this before reading it."""
        try:
            stat = transcript.stat()
        except OSError as exc:
            logger.warning(
                "session %s: transcript could not be examined (%s); re-ingesting",
                DisplayName(Transcript(transcript).document_name()).redacted(),
                type(exc).__name__,
            )
            return cls(transcript, "")
        return cls(transcript, f"{stat.st_size}:{stat.st_mtime_ns}")

    def document_name(self) -> str:
        """Return the ``session-<id[:8]>`` name the transcript is stored under."""
        return Transcript(self.path).document_name()
