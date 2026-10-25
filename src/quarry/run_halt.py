"""When a backfill run must stop, and the first reason it was given."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.backfill_writer import WriteOutcome

if TYPE_CHECKING:
    from quarry.stop_signal import StopSignal


@final
class RunHalt:
    """The decision to stop: a stop signal, an abort, or a queue that stays full.

    The first reason given is the one kept, so a run stopped by a daemon
    shutdown that is then cancelled still says it was the shutdown.  After
    :attr:`MAX_FULL` transcripts in a row that a full queue would not take, the
    queue is not draining and each further transcript would wait out the same
    deadline for the same answer; any other answer from the queue ends the run
    of them.
    """

    MAX_FULL: ClassVar[int] = 3

    __slots__ = ("_full", "_reason", "_stop")

    _full: int
    _reason: str
    _stop: StopSignal

    def __new__(cls, stop: StopSignal) -> Self:
        self = super().__new__(cls)
        self._stop = stop
        self._reason = ""
        self._full = 0
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
        self._full = 0

    def not_admitted(self) -> None:
        """Note a transcript the queue would not take; give up if they keep coming."""
        self._full += 1
        if self._full >= self.MAX_FULL:
            self.abort("ingest queue stayed full")

    def refused(self, outcome: WriteOutcome, reason: str) -> None:
        """Note how the queue answered a transcript it did not store.

        A queue that stayed full extends the streak; a job that had started when
        the run gave up (running, or past its commit) stops the run; any other
        answer ends the streak.
        """
        if outcome is WriteOutcome.NOT_ADMITTED:
            self.not_admitted()
        elif outcome in {WriteOutcome.ABORTED_RUNNING, WriteOutcome.ABORTED_COMMITTING}:
            self.abort(reason)
        else:
            self.answered()
