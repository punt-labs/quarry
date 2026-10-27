"""Why a queued job was refused or abandoned, for a producer that waits on it."""

from __future__ import annotations

from enum import Enum, auto
from typing import Self

from quarry.refusal import RefusalError


class IngestRefusal(Enum):
    """The ways waiting on the ingest queue ends without the job finishing."""

    SHUTTING_DOWN = auto()  # the daemon is stopping; nothing more is admitted
    NOT_ADMITTED = auto()  # the queue stayed full for the whole wait
    WORKER_STOPPED = auto()  # the collection's worker died before the job ended
    UNFINISHED = auto()  # admitted, but no terminal state by the deadline


class IngestRefusedError(RefusalError):
    """A job was refused or abandoned; the message is a stable reason."""

    _reason: IngestRefusal = IngestRefusal.NOT_ADMITTED

    @classmethod
    def because(cls, reason: IngestRefusal, message: str) -> Self:
        """Return the refusal for *reason*, worded as *message*, ready to raise."""
        refusal = cls(message)
        refusal._reason = reason
        return refusal

    @property
    def reason(self) -> IngestRefusal:
        """Return which way the wait ended."""
        return self._reason
