"""One deadline for the daemon's whole teardown, and the stop timeout above it."""

from __future__ import annotations

import math
import time
from typing import ClassVar, Self, final

from quarry.config import Settings


@final
class ShutdownBudget:
    """The time a daemon teardown may take, started when the teardown starts.

    The *total* is ``Settings.ingest_drain_timeout_s`` (30 s by default).  The
    queue close and the task drain share it: the queue gets what is left after the
    watch loop stops, and :class:`~quarry.daemon.task_drain.TaskDrain` gets what
    is left after the queue, its cancel wait inside that same remainder.  So the
    drains together take no longer than the total.

    The service manager must not kill the daemon before it has used that time, so
    the stop timeout baked into the unit and the plist (``TimeoutStopSec`` for
    systemd, ``ExitTimeOut`` for launchd) is the total plus :attr:`MARGIN_S`: 40 s
    by default, strictly more than the total.
    """

    # What the service manager allows beyond the teardown's own budget: the
    # process exit, the sidecar removal and the lock release that follow it.
    MARGIN_S: ClassVar[float] = 10.0

    __slots__ = ("_deadline", "_total_s")

    _deadline: float
    _total_s: float

    def __new__(cls, total_s: float) -> Self:
        self = super().__new__(cls)
        self._total_s = total_s
        self._deadline = time.monotonic() + total_s
        return self

    @classmethod
    def from_settings(cls) -> Self:
        """Return a budget of the configured total, started now."""
        return cls(Settings().ingest_drain_timeout_s)

    @property
    def total_s(self) -> float:
        """Return the whole budget, in seconds."""
        return self._total_s

    @property
    def stop_timeout_s(self) -> int:
        """Return the service stop timeout: the total and the margin, rounded up."""
        return math.ceil(self._total_s + self.MARGIN_S)

    def remaining(self) -> float:
        """Return what is left of the budget, never below zero."""
        return max(self._deadline - time.monotonic(), 0.0)
