"""When a backfill run must stop, and the first reason it was given."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.admission_streak import AdmissionStreak
from quarry.backfill_writer import WriteOutcome

if TYPE_CHECKING:
    from quarry.stop_signal import StopSignal


@final
class RunHalt:
    """The decision to stop: a stop signal, an abort, or a queue that stays full.

    The first reason given is the one kept, so a run stopped by a daemon
    shutdown that is then cancelled still says it was the shutdown.
    """

    __slots__ = ("_reason", "_stop", "_streak")

    _reason: str
    _stop: StopSignal
    _streak: AdmissionStreak

    def __new__(cls, stop: StopSignal) -> Self:
        self = super().__new__(cls)
        self._stop = stop
        self._reason = ""
        self._streak = AdmissionStreak()
        return self

    @property
    def reason(self) -> str:
        """Return why the run stopped, or ``""`` if it has not."""
        return self._reason

    def halted(self) -> bool:
        """Return whether the run must stop before its next step."""
        if self._stop.is_set():
            self.abort(self._stop.reason)
        return bool(self._reason)

    def abort(self, reason: str) -> None:
        """Stop the run for *reason* (the first one given is kept)."""
        self._reason = self._reason or reason

    def answered(self) -> None:
        """Note that a transcript went through the queue and was answered."""
        self._streak.answered()

    def not_admitted(self) -> None:
        """Note a transcript the queue would not take; give up if they keep coming."""
        if self._streak.refused():
            self.abort("ingest queue stayed full")

    def refused(self, outcome: WriteOutcome, reason: str) -> None:
        """Note how the queue answered a transcript it did not store.

        A queue that stayed full extends the streak; a job that had started when
        the run gave up stops the run; any other answer ends the streak.
        """
        if outcome is WriteOutcome.NOT_ADMITTED:
            self.not_admitted()
        elif outcome is WriteOutcome.ABORTED_RUNNING:
            self.abort(reason)
        else:
            self.answered()
