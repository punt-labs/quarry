"""The one module that imports ``watchdog``: a thin :class:`FsEventSource` adapter.

Everything else in the watch loop talks to the :class:`FsEventSource` Protocol,
so the library is isolated here (Decision 1: watchdog primary, its
``PollingObserver`` as the operator-selectable fallback).  The adapter owns one
observer thread for the whole process and translates watchdog's events into the
engine's :class:`FsEvent` value type.  Every observer-thread callback is wrapped
so a raising handler can never crash the observer thread (bug-class 2).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast, final

from watchdog.events import FileMovedEvent, FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from quarry.daemon.fs_events import FsEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from watchdog.observers.api import BaseObserver, ObservedWatch

logger = logging.getLogger(__name__)

# Bound the observer-thread join at shutdown so a wedged native watcher can never
# hang the daemon's teardown.
_JOIN_TIMEOUT_S = 5.0


@final
class _WatchdogHandler(FileSystemEventHandler):
    """Translate watchdog events into :class:`FsEvent` and forward them.

    A rename arrives as a ``FileMovedEvent``; it is delivered as a deletion of
    the source path and a modify of the destination path so the debouncer routes
    each half correctly.  Directory events are ignored — the loop indexes files.
    """

    __slots__ = ("_on_event",)

    _on_event: Callable[[FsEvent], None]

    def __new__(cls, on_event: Callable[[FsEvent], None]) -> Self:
        self = super().__new__(cls)
        self._on_event = on_event
        return self

    def on_any_event(self, event: FileSystemEvent) -> None:
        """Forward one file event; never propagate into the observer thread."""
        if event.is_directory:
            return
        try:
            if isinstance(event, FileMovedEvent):
                self._emit(event.src_path, deleted=True)
                self._emit(event.dest_path, deleted=False)
            elif event.event_type == "deleted":
                self._emit(event.src_path, deleted=True)
            elif event.event_type in {"created", "modified"}:
                self._emit(event.src_path, deleted=False)
        except Exception:
            logger.exception("watch: event handler failed for %r", event)

    def _emit(self, raw_path: str | bytes, *, deleted: bool) -> None:
        """Normalize a watchdog path to :class:`FsEvent` and forward it."""
        path = raw_path.decode() if isinstance(raw_path, bytes) else raw_path
        self._on_event(FsEvent(Path(path), deleted=deleted))


@final
class WatchdogSource:
    """One watchdog observer thread behind the :class:`FsEventSource` Protocol.

    ``use_polling`` selects the stat-walk ``PollingObserver`` — the zero-inotify,
    zero-FSEvents fallback an operator sets for large trees (or where the native
    watcher is unavailable).  A per-tree ``schedule`` that raises ``OSError``
    (inotify watch exhaustion, ``ENOSPC``) is logged and skipped rather than
    aborting the whole loop; that tree reconciles on the next full scan.
    """

    __slots__ = ("_observer",)

    _observer: BaseObserver

    def __new__(
        cls, *, use_polling: bool = False, poll_interval_s: float = 2.0
    ) -> Self:
        self = super().__new__(cls)
        observer: BaseObserver = (
            PollingObserver(timeout=poll_interval_s) if use_polling else Observer()
        )
        observer.start()
        self._observer = observer
        return self

    def schedule(
        self, root: Path, on_event: Callable[[FsEvent], None]
    ) -> object | None:
        """Begin watching *root* recursively; ``None`` if the tree cannot be watched.

        Returns the watchdog ``ObservedWatch`` handle, or ``None`` when the OS
        refuses the watch (e.g. ``ENOSPC`` on inotify exhaustion) — the caller
        treats a ``None`` handle as "this tree is unwatched, rely on scans".
        """
        try:
            return self._observer.schedule(
                _WatchdogHandler(on_event), str(root), recursive=True
            )
        except OSError as exc:
            logger.warning("watch: cannot watch %s (%s); relying on scans", root, exc)
            return None

    def unschedule(self, handle: object | None) -> None:
        """Stop watching the tree associated with *handle* (a no-op if ``None``)."""
        if handle is not None:
            self._observer.unschedule(cast("ObservedWatch", handle))

    def stop(self) -> None:
        """Stop the observer and join its thread under a bounded timeout."""
        self._observer.stop()
        self._observer.join(timeout=_JOIN_TIMEOUT_S)
