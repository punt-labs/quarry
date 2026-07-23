"""Adapter test: the real watchdog observer emits FsEvents against tmp_path.

Everything else drives a synthetic source; this proves the one module that
imports watchdog actually delivers create/modify/delete as :class:`FsEvent`s.
The stat-walk ``PollingObserver`` is used deterministically (a short poll
interval) so the test is not subject to native-watcher timing flakiness.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.fs_watchdog import WatchdogSource

if TYPE_CHECKING:
    from quarry.daemon.fs_events import FsEvent

_DEADLINE_S = 5.0
_POLL_S = 0.05


@final
class _Recorder:
    """Collect events the observer thread delivers (thread-safe list append)."""

    __slots__ = ("events",)

    events: list[FsEvent]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.events = []
        return self

    def __call__(self, event: FsEvent) -> None:
        self.events.append(event)

    def wait_for(self, predicate: object, deadline: float = _DEADLINE_S) -> bool:
        """Poll until *predicate* matches a recorded event, or the deadline."""
        assert callable(predicate)
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if any(predicate(event) for event in self.events):
                return True
            time.sleep(_POLL_S)
        return False


def test_watchdog_source_reports_create_modify_and_delete(tmp_path: Path) -> None:
    """A created-then-modified-then-deleted file surfaces as FsEvents."""
    recorder = _Recorder()
    source = WatchdogSource(use_polling=True, poll_interval_s=0.1)
    handle = source.schedule(tmp_path, recorder)
    try:
        target = tmp_path / "note.md"
        target.write_text("first")
        assert recorder.wait_for(lambda e: e.path == target and not e.deleted), (
            "create/modify event never arrived"
        )

        target.unlink()
        assert recorder.wait_for(lambda e: e.path == target and e.deleted), (
            "delete event never arrived"
        )
    finally:
        source.unschedule(handle)
        source.stop()


def test_watchdog_source_stop_is_idempotent_after_unschedule(tmp_path: Path) -> None:
    """Unscheduling then stopping tears the observer down without error."""
    source = WatchdogSource(use_polling=True, poll_interval_s=0.1)
    handle = source.schedule(tmp_path, _Recorder())
    source.unschedule(handle)
    source.stop()  # joins the observer thread under the bounded timeout
