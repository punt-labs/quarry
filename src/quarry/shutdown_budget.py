"""The running clock of one daemon teardown."""

from __future__ import annotations

import time
from typing import Self, final


@final
class ShutdownBudget:
    """The time a running teardown has left, started when the teardown starts.

    How the total is shared between the steps is
    :class:`~quarry.shutdown_policy.ShutdownPolicy`; this is only the clock.
    """

    __slots__ = ("_deadline",)

    _deadline: float

    def __new__(cls, total_s: float) -> Self:
        self = super().__new__(cls)
        self._deadline = time.monotonic() + total_s
        return self

    def remaining(self) -> float:
        """Return what is left of the budget, never below zero."""
        return max(self._deadline - time.monotonic(), 0.0)
