"""Daemon-lifetime state that does not open storage at construction."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.watch_loop import WatchLoop
from quarry.recall_telemetry import RecallTelemetry

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext


@final
class DaemonRuntime:
    """Bind watch and recall lifecycles to a single daemon instance."""

    __slots__ = ("_ready", "_recall", "_start_time", "_watch_loop")

    _watch_loop: WatchLoop
    _recall: RecallTelemetry
    _start_time: float
    _ready: bool

    def __new__(cls, context: DaemonContext) -> Self:
        """Create dormant services safe to construct before the event loop."""
        self = super().__new__(cls)
        self._watch_loop = WatchLoop(context)
        self._recall = RecallTelemetry(context.settings)
        self._start_time = time.monotonic()
        self._ready = False
        return self

    @property
    def watch_loop(self) -> WatchLoop:
        """Return the resident filesystem watcher."""
        return self._watch_loop

    @property
    def recall(self) -> RecallTelemetry:
        """Return the resident recall facade."""
        return self._recall

    @property
    def uptime_seconds(self) -> float:
        """Return monotonic time elapsed since this instance was created."""
        return time.monotonic() - self._start_time

    @property
    def state(self) -> str:
        """Return the daemon's health state."""
        return "ready" if self._ready else "starting"

    def mark_ready(self) -> None:
        """Publish readiness after resources have warmed."""
        self._ready = True
