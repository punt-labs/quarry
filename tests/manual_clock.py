"""A loop clock the test advances by hand, so a timer never depends on scheduling."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, cast, final

if TYPE_CHECKING:
    import asyncio


@final
class ManualTimer:
    """A timer handle that fires only when the clock is advanced past its due time."""

    __slots__ = ("_args", "_callback", "_cancelled", "_due")

    _args: tuple[object, ...]
    _callback: object
    _cancelled: bool
    _due: float

    def __new__(cls, due: float, callback: object, args: tuple[object, ...]) -> Self:
        self = super().__new__(cls)
        self._due = due
        self._callback = callback
        self._args = args
        self._cancelled = False
        return self

    @property
    def due(self) -> float:
        """Return the clock reading at which this timer fires."""
        return self._due

    def cancel(self) -> None:
        self._cancelled = True

    def cancelled(self) -> bool:
        return self._cancelled

    def fire(self) -> None:
        """Run the callback once; a fired timer is spent."""
        self._cancelled = True
        assert callable(self._callback)
        self._callback(*self._args)


@final
class ManualLoop:
    """The slice of an event loop a ``FinalizeThrottle`` uses, on a manual clock."""

    __slots__ = ("_now", "_timers")

    _now: float
    _timers: list[ManualTimer]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._now = 0.0
        self._timers = []
        return self

    @property
    def as_loop(self) -> asyncio.AbstractEventLoop:
        """Return this object typed as the loop the throttle expects."""
        return cast("asyncio.AbstractEventLoop", self)

    def time(self) -> float:
        return self._now

    def call_later(self, delay: float, callback: object, *args: object) -> ManualTimer:
        timer = ManualTimer(self._now + delay, callback, args)
        self._timers.append(timer)
        return timer

    def live(self) -> list[ManualTimer]:
        """Return the timers that are armed and not yet fired or cancelled."""
        return [t for t in self._timers if not t.cancelled()]

    def set_time(self, now: float) -> None:
        """Move the clock without firing anything."""
        self._now = now

    def advance(self, seconds: float) -> None:
        """Move the clock forward, firing each timer that comes due, earliest first."""
        target = self._now + seconds
        while due := [t for t in self.live() if t.due <= target]:
            timer = min(due, key=lambda t: t.due)
            self._now = max(self._now, timer.due)
            timer.fire()
        self._now = target
