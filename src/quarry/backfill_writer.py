"""Why a backfilled transcript was not stored: the writer's failure and its outcome."""

from __future__ import annotations

from enum import Enum, auto
from typing import Self

from quarry.refusal import RefusalError


class WriteOutcome(Enum):
    """Why a transcript was not stored, as far as the run's next step cares."""

    FAILED = auto()  # this transcript only; the run goes on
    UNINDEXABLE = auto()  # nothing to index in it; storing it again changes nothing
    NOT_ADMITTED = auto()  # the queue had no room within the wait
    ABORTED = auto()  # the run cannot go on; the transcript's job will never run
    ABORTED_RUNNING = auto()  # ... and the job had started: it stops at its commit
    ABORTED_COMMITTING = auto()  # ... and it is past its commit: stored, or failed


class BackfillWriteError(RefusalError):
    """A transcript was not stored; the message is the writer's URL-free reason."""

    _outcome: WriteOutcome = WriteOutcome.FAILED

    def as_outcome(self, outcome: WriteOutcome) -> Self:
        """Say which kind of not-stored this is; return the error, ready to raise."""
        self._outcome = outcome
        return self

    @property
    def outcome(self) -> WriteOutcome:
        """Return what the run should make of this failure."""
        return self._outcome
