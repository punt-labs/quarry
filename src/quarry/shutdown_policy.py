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
    - the task drain gets what is left, and reserves :attr:`CANCEL_SHARE` of that
      for its cancel wait.

    The service manager must not kill the daemon before it has used its budget:
    the stop timeout baked into the unit and the plist (``TimeoutStopSec`` for
    systemd, ``ExitTimeOut`` for launchd) is the total plus :attr:`MARGIN_S`.
    The policy has no clock; :class:`~quarry.shutdown_budget.ShutdownBudget` is
    the clock a running teardown reads.
    """

    # Beyond the teardown's own budget: the process exit, the sidecar removal and
    # the lock release that follow it.
    MARGIN_S: ClassVar[float] = 10.0
    WATCH_SHARE: ClassVar[float] = 0.2
    QUEUE_MIN_SHARE: ClassVar[float] = 0.5
    SCAN_SHARE: ClassVar[float] = 0.4
    SCAN_CAP_S: ClassVar[float] = 30.0
    CANCEL_SHARE: ClassVar[float] = 0.25
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

    def env_line(self) -> str:
        """Return the ``NAME="value"`` line that carries the total to the daemon."""
        return f'{self.ENV_NAME}="{self._total_s:g}"'
