"""Timestamped event window for convention hint detection.

A rolling buffer of recent tool calls, persisted to JSON so state
survives across hook invocations within a session.  Adapted from
biff's ``DisplayQueue`` pattern: time-based expiry, injected clock
for deterministic testing, fail-open deserialization.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class Clock(Protocol):
    """Callable that returns wall-clock seconds (``time.time`` signature)."""

    def __call__(self) -> float: ...


@dataclass(frozen=True)
class ToolEvent:
    """A single tool invocation record."""

    ts: float
    tool: str
    command: str


class HintAccumulator:
    """Rolling window of recent tool events with TTL-based expiry.

    Parameters
    ----------
    ttl:
        Seconds before an event expires.  Default 300 (5 minutes).
    max_events:
        Hard cap on stored events to prevent unbounded growth.
    clock:
        Injectable clock for deterministic testing.
    """

    def __init__(
        self,
        *,
        ttl: float = 300.0,
        max_events: int = 50,
        clock: Clock = time.time,
    ) -> None:
        self._ttl = ttl
        self._max_events = max_events
        self._clock = clock
        self._events: list[ToolEvent] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, event: ToolEvent) -> None:
        """Append *event* and prune expired / overflow entries."""
        self._events.append(event)
        self._purge_expired()

    def recent(self, n: int = 10) -> list[ToolEvent]:
        """Return the last *n* events after pruning."""
        self._purge_expired()
        return self._events[-n:]

    def to_json(self) -> str:
        """Serialize current state to a JSON string."""
        return json.dumps(
            [{"ts": e.ts, "tool": e.tool, "command": e.command} for e in self._events]
        )

    @classmethod
    def from_json(
        cls,
        data: str,
        *,
        ttl: float = 300.0,
        max_events: int = 50,
        clock: Clock = time.time,
    ) -> HintAccumulator:
        """Deserialize from JSON.  Returns an empty accumulator on any error."""
        acc = cls(ttl=ttl, max_events=max_events, clock=clock)
        try:
            raw = json.loads(data)
            if not isinstance(raw, list):
                return acc
            for item in raw:
                if not isinstance(item, dict):
                    continue
                ts = item.get("ts")
                tool = item.get("tool")
                command = item.get("command")
                if (
                    isinstance(ts, (int, float))
                    and isinstance(tool, str)
                    and isinstance(command, str)
                ):
                    acc._events.append(
                        ToolEvent(ts=float(ts), tool=tool, command=command)
                    )
        except (json.JSONDecodeError, TypeError, KeyError):
            logger.debug("hint-accumulator: corrupt JSON, returning empty")
            return cls(ttl=ttl, max_events=max_events, clock=clock)
        acc._purge_expired()
        return acc

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _purge_expired(self) -> None:
        """Remove events older than TTL and enforce max cap."""
        cutoff = self._clock() - self._ttl
        self._events = [e for e in self._events if e.ts >= cutoff]
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events :]
