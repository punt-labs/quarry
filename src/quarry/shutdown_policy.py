"""How a daemon's teardown budget is shared, and the service stop timeout above it."""

from __future__ import annotations

import math
from typing import ClassVar, Self, final

from quarry.config import Settings


@final
class ShutdownPolicy:
    """The rules of a teardown, as pure functions of its total budget.

    The *total* is ``Settings.ingest_drain_timeout_s``.  The teardown spends it in
    steps, each with a stated share:

    - the watch-loop stop gets at most :attr:`WATCH_SHARE` of it;
    - the queue drain is guaranteed at least :attr:`QUEUE_MIN_SHARE` of it;
    - a backfill's scan thread is given up on :attr:`SCAN_SHARE` of it after the
      stop (never longer than :attr:`SCAN_CAP_S`), so a stuck scan normally ends
      inside the task drain that follows;
    - the task drain gets what is left (never less than :attr:`TASK_FLOOR_S`), and
      reserves :attr:`CANCEL_SHARE` of that for its cancel wait.

    The service manager must not kill the daemon before it has used its budget:
    the stop timeout baked into the unit and the plist (``TimeoutStopSec`` for
    systemd, ``ExitTimeOut`` for launchd) is the total plus :attr:`MARGIN_S`.
    The policy has no clock; :class:`~quarry.shutdown_budget.ShutdownBudget` is
    the clock a running teardown reads.
    """

    # Beyond the teardown's own budget: uvicorn's graceful phase, the queue's abort
    # wait, the task drain's floor, and the process exit.  The margin must cover
    # their sum (see :attr:`GRACEFUL_S`).
    MARGIN_S: ClassVar[float] = 12.0
    # The most an aborted queue waits for its workers to unwind, beyond the budget.
    ABORT_WAIT_S: ClassVar[float] = 5.0
    # The least the task drain waits, even with the budget spent, so a task that
    # finishes just after the queue abort is not cancelled.  Taken from the margin.
    TASK_FLOOR_S: ClassVar[float] = 1.0
    # What is left of the margin for the process exit itself.
    EXIT_SLACK_S: ClassVar[float] = 1.0
    # What the margin keeps unspent: the worst case is this far below the stop
    # timeout, never at it.
    HEADROOM_S: ClassVar[float] = 2.0
    WATCH_SHARE: ClassVar[float] = 0.2
    QUEUE_MIN_SHARE: ClassVar[float] = 0.5
    SCAN_SHARE: ClassVar[float] = 0.4
    SCAN_CAP_S: ClassVar[float] = 30.0
    CANCEL_SHARE: ClassVar[float] = 0.25
    # How long uvicorn drains open connections before it runs the lifespan
    # teardown.  It is outside the budget, so it is what the margin has left once
    # the abort wait, the task floor and the exit slack are paid: the worst case,
    # graceful phase plus budget plus abort wait plus task floor, stays inside
    # the total plus the margin.
    GRACEFUL_S: ClassVar[int] = int(
        MARGIN_S - ABORT_WAIT_S - TASK_FLOOR_S - EXIT_SLACK_S - HEADROOM_S
    )
    # The environment variable the installed daemon reads the total from, so the
    # stop timeout baked at install and the budget the daemon spends are one number.
    ENV_NAME: ClassVar[str] = "INGEST_DRAIN_TIMEOUT_S"

    __slots__ = ("_total_s",)

    _total_s: float

    def __new__(cls, total_s: float) -> Self:
        self = super().__new__(cls)
        self._total_s = total_s
        return self

    @classmethod
    def from_settings(cls) -> Self:
        """Return the policy for the configured total."""
        return cls(Settings().ingest_drain_timeout_s)

    @staticmethod
    def stop_timeout_for(total_s: float) -> int:
        """Return the service stop timeout for a *total_s* budget, rounded up."""
        return math.ceil(total_s + ShutdownPolicy.MARGIN_S)

    @property
    def total_s(self) -> float:
        """Return the whole budget, in seconds."""
        return self._total_s

    @property
    def stop_timeout_s(self) -> int:
        """Return the service stop timeout for this total."""
        return self.stop_timeout_for(self._total_s)

    @property
    def watch_s(self) -> float:
        """Return the most the watch-loop stop may take."""
        return self._total_s * self.WATCH_SHARE

    @property
    def queue_min_s(self) -> float:
        """Return the least the queue drain is guaranteed."""
        return self._total_s * self.QUEUE_MIN_SHARE

    @property
    def scan_bound_s(self) -> float:
        """Return how long after a stop a scan thread is waited for."""
        return min(self.SCAN_CAP_S, self._total_s * self.SCAN_SHARE)

    @staticmethod
    def total_bounds() -> tuple[float, float]:
        """Return the least and the most a configured total may be, in seconds.

        Read off the setting itself, so the bounds an installed definition is
        checked against are the ones the daemon would refuse it by.
        """
        metadata = Settings.model_fields["ingest_drain_timeout_s"].metadata
        low = next(m.ge for m in metadata if hasattr(m, "ge"))
        high = next(m.le for m in metadata if hasattr(m, "le"))
        return low, high

    @classmethod
    def cancel_s(cls, timeout: float) -> float:
        """Return the part of a task-drain *timeout* reserved for the cancel wait."""
        return timeout * cls.CANCEL_SHARE

    def env_line(self) -> str:
        """Return the ``NAME="value"`` line that carries the total to the daemon."""
        return f'{self.ENV_NAME}="{self._total_s:g}"'
