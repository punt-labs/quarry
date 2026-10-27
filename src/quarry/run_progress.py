"""What a backfill run has counted so far, readable from outside its thread."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.backfill_stats import BackfillStats

if TYPE_CHECKING:
    from quarry.backfill_tally import BackfillTally
    from quarry.stop_signal import StopSignal


@final
class RunProgress:
    """A window onto a run's tally, for whoever has to report it part-way.

    The scan thread attaches its tally once the registrations are read; until
    then there is nothing counted.  A snapshot read from another thread is
    approximate by a transcript at most: it is what the run had counted when it
    was asked, with the stop's reason in it, which is all a task that gave up on
    a stuck thread can say.
    """

    __slots__ = ("_skipped_unregistered", "_stop", "_tally")

    _skipped_unregistered: int
    _stop: StopSignal
    _tally: BackfillTally | None  # None until the scan thread has attached its own

    def __new__(cls, stop: StopSignal) -> Self:
        self = super().__new__(cls)
        self._stop = stop
        self._tally = None
        self._skipped_unregistered = 0
        return self

    def attach(self, tally: BackfillTally, skipped_unregistered: int) -> None:
        """Take *tally* as the run's counts; *skipped_unregistered* is known by now."""
        self._skipped_unregistered = skipped_unregistered
        self._tally = tally

    def snapshot(self) -> BackfillStats:
        """Return the stats as counted so far, naming the stop if there is one."""
        if self._tally is None:
            reason = self._stop.reason
            line = [f"backfill stopped: {reason}"] if reason else []
            return BackfillStats(stopped=reason, errors=line)
        self._tally.halted()  # takes the stop's reason into the tally
        return self._tally.stats(self._skipped_unregistered)
