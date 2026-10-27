"""Track whether a retention prune is due, on its own schedule.

Split out of ``query_log.py`` (PY-IC-6 single responsibility): "is it time to
prune again" is a scheduling policy distinct from owning the connection,
recording rows, and running the prune's SQL, and giving it a dedicated,
narrow set of attributes keeps :class:`~quarry.query_log.QueryLog`'s own
methods from touching a sprawling, partly-disjoint attribute set.
"""

from __future__ import annotations

import time
from typing import Self, final


@final
class PruneSchedule:
    """Decide whether :meth:`due` should trigger another prune, on a cadence.

    Unconfigured (the default) means "no periodic retention configured" --
    :meth:`due` always returns ``None`` until :meth:`configure` is called, so
    a bare :class:`~quarry.query_log.QueryLog` (every test in that module,
    and :meth:`~quarry.query_log.QueryLog.prune`'s own direct callers) never
    triggers a prune as a ``record()`` side effect.
    """

    __slots__ = ("_cadence_s", "_last_monotonic", "_retention_days")

    _retention_days: int | None
    _cadence_s: float
    _last_monotonic: float

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._retention_days = None
        self._cadence_s = 0.0
        self._last_monotonic = 0.0
        return self

    def configure(self, retention_days: int, cadence_s: float) -> None:
        """Arm the schedule: *retention_days* to prune to, every *cadence_s*."""
        self._retention_days = retention_days
        self._cadence_s = cadence_s
        self._last_monotonic = time.monotonic()

    def due(self) -> int | None:
        """Return the retention window if a prune is due now, else ``None``.

        A ``True`` result also resets the clock -- the caller is expected to
        actually prune on a non-``None`` return, the same way a token-bucket
        take is expected to be spent.
        """
        if self._retention_days is None:
            return None
        now = time.monotonic()
        if now - self._last_monotonic < self._cadence_s:
            return None
        self._last_monotonic = now
        return self._retention_days
