"""The :class:`FsEventSource` adapter over the ``watchdog`` library.

Everything else in the watch loop talks to the :class:`FsEventSource` Protocol
(:mod:`~quarry.daemon.fs_events`), so watchdog stays confined to this module
and its Linux collaborator :mod:`~quarry.daemon.inotify_prune_chain`.

One recursive watch per registered root (DES-045e — supersedes DES-045d's
per-directory scheduling, which was structurally wrong on Linux: every
``ObservedWatch`` gets its own emitter, and each Linux emitter's ``Inotify``
wrapper calls the libc ``inotify_init()`` — one kernel INSTANCE plus threads
and fds per directory. ``fs.inotify.max_user_instances`` defaults to 128
(per-user, shared with IDEs/editors), so per-directory scheduling exhausted
it at ~120 directories).

``WatchdogSource`` instead picks the observer by platform (a module-scope
``platform.system()`` branch at the top of this file, so the import happens
once at process startup, not per-``WatchdogSource``): on Linux, an import
failure is loud (``logger.error``) and falls back rather than silently
reverting to unpruned recursive watching, so a future watchdog release that
renames the private internals :mod:`~quarry.daemon.inotify_prune`/
``inotify_prune_chain`` depend on is visible in the daemon's logs, not just
in a quietly-larger fd/watch footprint.

* **Linux** — :class:`~quarry.daemon.inotify_prune_chain.PrunedInotifyObserver`:
  one inotify instance per root (comfortably inside
  ``max_user_instances``), whose internal directory walk skips ignored
  directories per the shared :mod:`~quarry.sync_discovery` ignore spec — so
  the much larger ``max_user_watches`` budget (65,536, per-descriptor, no
  per-user cap) is spent only on directories worth watching.
* **macOS and other platforms** — watchdog's standard recursive observer
  (FSEvents on macOS: one stream per root, no per-directory kernel cost at
  all, so nothing to prune at the schedule layer).
* **``use_polling=True``** (either platform) — the stat-walk
  ``PollingObserver``, an operator opt-out for large trees or where the
  native watcher is unavailable.

On the non-Linux and polling paths, ignore filtering happens where it always
has: the post-debounce submitter filter
(:class:`~quarry.daemon.watch_submit.WatchSubmitter`) applies the SAME
ignore spec before any event reaches the ingest queue.
"""

from __future__ import annotations

import logging
import platform
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast, final

from watchdog.events import FileMovedEvent, FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from watchdog.utils import UnsupportedLibcError

from quarry.daemon.fs_events import FsEvent
from quarry.ingestion.pipeline import SUPPORTED_EXTENSIONS
from quarry.sync_discovery import FileDiscovery

if TYPE_CHECKING:
    from collections.abc import Callable

    from watchdog.observers.api import BaseObserver, ObservedWatch

    from quarry.daemon.inotify_prune_chain import (
        PrunedInotifyObserver as _PrunedInotifyObserverType,
    )

logger = logging.getLogger(__name__)

# Bound the observer-thread join at shutdown so a wedged native watcher can never
# hang the daemon's teardown.
_JOIN_TIMEOUT_S = 5.0

# The pruned Linux observer, or None off Linux -- imported at module scope
# (not lazily inside a function) so this is a plain conditional import, not a
# PLC0415 violation, and so pyright/mypy narrow the None-check below rather
# than needing a suppression.  Branches explicitly on platform.system() (djb
# major): a Linux host whose import fails (e.g. a future watchdog release
# renaming the private internals this module subclasses) is loud and
# distinct from the expected, silent macOS/Windows fallback -- an
# `except (ImportError, UnsupportedLibcError)` with no platform check would
# make "watchdog upgrade broke us" indistinguishable from "this is macOS",
# silently reverting a Linux host to UNPRUNED recursive watching with no
# signal at all.
_PrunedInotifyObserver: type[_PrunedInotifyObserverType] | None
if platform.system() == "Linux":
    try:
        from quarry.daemon.inotify_prune_chain import (
            PrunedInotifyObserver as _PrunedInotifyObserver,
        )
    except (ImportError, UnsupportedLibcError) as exc:
        logger.error(
            "watch: Linux pruned inotify observer unavailable (%s); "
            "falling back to UNPRUNED recursive watching",
            exc,
        )
        _PrunedInotifyObserver = None
else:
    _PrunedInotifyObserver = None


@final
class _WatchdogHandler(FileSystemEventHandler):
    """Translate watchdog events into :class:`FsEvent` and forward them.

    A rename arrives as a ``FileMovedEvent``; it is delivered as a deletion of
    the source path and a modify of the destination path so the debouncer routes
    each half correctly.  Directory events are ignored — the loop indexes files,
    and directory-level pruning (Linux) or the post-debounce filter (elsewhere)
    already decides what is watched at all.
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
        """Forward a supported-suffix path as an :class:`FsEvent` (cheap pre-filter).

        Filtering by suffix here, on the observer thread, keeps unsupported-file
        churn out of the debouncer; the authoritative resolve/ignore filter runs
        post-debounce in the submitter.
        """
        path = Path(raw_path.decode() if isinstance(raw_path, bytes) else raw_path)
        if path.suffix.lower() in SUPPORTED_EXTENSIONS:
            self._on_event(FsEvent(path, deleted=deleted))


@final
class WatchdogSource:
    """One watchdog observer thread behind the :class:`FsEventSource` Protocol.

    ``use_polling`` selects the stat-walk ``PollingObserver`` — the zero-inotify,
    zero-FSEvents fallback an operator sets for large trees (or where the native
    watcher is unavailable).
    """

    __slots__ = ("_observer",)

    _observer: BaseObserver

    def __new__(
        cls, *, use_polling: bool = False, poll_interval_s: float = 2.0
    ) -> Self:
        self = super().__new__(cls)
        observer = cls._build_observer(
            use_polling=use_polling, poll_interval_s=poll_interval_s
        )
        observer.start()
        self._observer = observer
        return self

    @staticmethod
    def _build_observer(*, use_polling: bool, poll_interval_s: float) -> BaseObserver:
        """Return the platform-appropriate, not-yet-started observer (DES-045e).

        Picks the pruned Linux observer when the module-scope import at the
        top of this file succeeded (see ``_PrunedInotifyObserver``); falls
        back to watchdog's standard recursive observer everywhere else.
        Logs the choice at ``INFO`` (djb minor) so "which watcher am I
        running" is one grep-able line in the daemon's own log, rather than
        something only inferable from platform + the earlier import-failure
        ``logger.error`` (which fires only when the pruned path was tried
        and failed, not on every startup).
        """
        if use_polling:
            logger.info("watch: using PollingObserver (use_polling=True)")
            return PollingObserver(timeout=poll_interval_s)
        if _PrunedInotifyObserver is not None:
            logger.info("watch: using PrunedInotifyObserver (Linux, pruned)")
            return _PrunedInotifyObserver.create()
        logger.info("watch: using watchdog's standard recursive Observer")
        return Observer()

    def schedule(
        self, root: Path, on_event: Callable[[FsEvent], None]
    ) -> object | None:
        """Begin watching *root* recursively; ``None`` if the tree cannot be watched.

        Refuses up front (before touching the observer) when *root* is
        unresolvable or is a refused temp/scratch tree
        (:attr:`~quarry.sync_discovery.FileDiscovery.root_available`) — the
        same guard the bulk scan applies, so a stale scratch registration is
        never watched by any observer. Otherwise returns the watchdog
        ``ObservedWatch`` handle, or ``None`` when the OS refuses the watch
        (e.g. inotify instance/watch exhaustion) — the caller treats a
        ``None`` handle as "this tree is unwatched, rely on scans".

        Building a :class:`~quarry.sync_discovery.FileDiscovery` compiles
        the tree's ignore spec, which is written to never raise for a
        malformed ignore file
        (:meth:`~quarry.ignore_spec.IgnoreRules._compile_lines`); this is
        caught anyway (djb/rev-silent finding) because this method's whole
        contract is "never crash the caller, return ``None`` instead" — a
        second layer here is cheap and this is the one boundary a
        surprising ignore-spec failure would otherwise reach
        (:meth:`~quarry.daemon.watch_loop.WatchLoop.start` has no
        try/except around ``schedule``).
        """
        try:
            discovery = FileDiscovery(root)
        except Exception:
            logger.exception(
                "watch: cannot evaluate ignore rules for %s; relying on scans", root
            )
            return None
        if not discovery.root_available:
            logger.info("watch: refusing scratch/unresolvable root %s", root)
            return None
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

    def is_alive(self, handle: object | None) -> bool:
        """Whether *handle*'s background emitter thread is still running (djb minor).

        ``schedule()`` returning a handle only proves the watch was
        installed at that moment — it says nothing about later: an
        unhandled exception on the emitter's own thread (this module now
        hardens the ignore-spec paths against that, but a future watchdog
        internals change is exactly the kind of surprise
        ``test_pins_the_watchdog_internals_this_module_subclasses`` exists
        to catch, not prevent) leaves the thread dead while the handle
        still looks "present" to every other check. Each scheduled tree
        gets its own ``EventEmitter`` thread; ``BaseObserver.emitters``
        (public) plus ``EventEmitter.watch`` (public) find the one for
        *handle* without reaching into ``BaseObserver``'s private
        ``_emitter_for_watch`` map.

        Thread-confinement invariant this iteration depends on:
        ``BaseObserver.emitters`` returns the live ``self._emitters`` set
        with NO lock -- ``schedule()``/``unschedule()`` are the only
        mutators and both take ``BaseObserver._lock`` around their
        mutation, but this read does not. That is safe only because every
        current caller (``WatchLoop.watch_state`` -> ``WatchRoster.
        watch_handle_alive`` -> here, and the roster's own ``watch``/
        ``unwatch``) runs on the asyncio event-loop thread, the SAME
        thread that calls ``schedule``/``unschedule`` -- so this read and
        those mutations can never interleave. Wrapping ``watch_state`` in
        ``run_in_threadpool`` (as its siblings in
        ``daemon/routes/registrations.py`` are) would break this and
        create a real set-changed-during-iteration race; do not do that
        without adding locking here first.
        """
        if handle is None:
            return False
        watch = cast("ObservedWatch", handle)
        return any(
            emitter.watch == watch and emitter.is_alive()
            for emitter in self._observer.emitters
        )

    def stop(self) -> None:
        """Stop the observer and join its thread under a bounded timeout."""
        self._observer.stop()
        self._observer.join(timeout=_JOIN_TIMEOUT_S)
