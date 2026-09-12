"""Adapter test: the real watchdog observer emits FsEvents against tmp_path.

Everything else drives a synthetic source; this proves the one module that
imports watchdog actually delivers create/modify/delete as :class:`FsEvent`s.
The stat-walk ``PollingObserver`` is used deterministically (a short poll
interval) so the test is not subject to native-watcher timing flakiness.
Directory-level ignore pruning is Linux-inotify-specific (DES-045e) and is
covered in ``tests/test_inotify_prune.py``; the ``PollingObserver`` and
non-Linux paths here rely on the post-debounce submitter filter, same as
before DES-045d.
"""

from __future__ import annotations

import importlib
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Self, final
from unittest.mock import patch

from quarry.daemon import fs_watchdog
from quarry.daemon.fs_watchdog import WatchdogSource

if TYPE_CHECKING:
    import pytest
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers.api import ObservedWatch

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
    source.stop()


def test_is_alive_true_for_a_live_scheduled_handle(tmp_path: Path) -> None:
    """A just-scheduled handle's emitter thread reports alive (djb minor)."""
    source = WatchdogSource(use_polling=True, poll_interval_s=0.1)
    try:
        handle = source.schedule(tmp_path, _Recorder())
        assert handle is not None
        assert source.is_alive(handle) is True
    finally:
        source.stop()


def test_is_alive_false_after_unschedule(tmp_path: Path) -> None:
    """Unscheduling retires the emitter -- is_alive must not still say True."""
    source = WatchdogSource(use_polling=True, poll_interval_s=0.1)
    try:
        handle = source.schedule(tmp_path, _Recorder())
        assert handle is not None
        source.unschedule(handle)
        assert source.is_alive(handle) is False
    finally:
        source.stop()


def test_is_alive_false_for_none_and_foreign_handles() -> None:
    source = WatchdogSource(use_polling=True, poll_interval_s=0.1)
    try:
        assert source.is_alive(None) is False
        assert source.is_alive(object()) is False  # never issued by this source
    finally:
        source.stop()


def test_schedule_failure_returns_none_and_spares_other_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schedule() failure for one root returns None and does not affect another.

    One recursive watch per root (DES-045e) means a schedule failure is a
    single all-or-nothing call — there is no partial per-directory state to
    release — but a failure on one registration must still leave a wholly
    separate registration on the SAME observer unaffected.
    """
    good = tmp_path / "good"
    good.mkdir()
    bad = tmp_path / "bad"
    bad.mkdir()

    source = WatchdogSource(use_polling=True, poll_interval_s=0.1)
    real_schedule = source._observer.schedule

    def flaky_schedule(
        handler: FileSystemEventHandler, path: str, *, recursive: bool = False
    ) -> ObservedWatch:
        if Path(path) == bad:
            msg = "No space left on device"
            raise OSError(28, msg)
        return real_schedule(handler, path, recursive=recursive)

    monkeypatch.setattr(source._observer, "schedule", flaky_schedule)

    good_recorder = _Recorder()
    good_handle = source.schedule(good, good_recorder)
    assert good_handle is not None

    bad_recorder = _Recorder()
    assert source.schedule(bad, bad_recorder) is None

    try:
        target = good / "note.md"
        target.write_text("still watched")
        assert good_recorder.wait_for(lambda e: e.path == target and not e.deleted), (
            "the unrelated good tree stopped receiving events after the bad "
            "tree's schedule failure"
        )
    finally:
        source.unschedule(good_handle)
        source.stop()


def test_schedule_refuses_an_excluded_scratch_root(tmp_path: Path) -> None:
    """A root that is itself a repo's own .tmp scratch is refused, not scheduled."""
    (tmp_path / ".git").mkdir()
    root = tmp_path / ".tmp" / "pytest-of-me" / "docs"
    root.mkdir(parents=True)
    source = WatchdogSource(use_polling=True, poll_interval_s=0.1)
    try:
        assert source.schedule(root, _Recorder()) is None
    finally:
        source.stop()


def test_schedule_survives_a_malformed_gitignore_at_the_registered_root(
    tmp_path: Path,
) -> None:
    """A malformed .gitignore in a tree being registered must not crash
    daemon startup / the register route (djb/rev-silent finding) -- the
    tree is still watched, not degraded, since ignore_spec.IgnoreRules now
    compiles line-tolerantly rather than raising.
    """
    (tmp_path / ".gitignore").write_text("*.log\n!\ndata/\n")
    source = WatchdogSource(use_polling=True, poll_interval_s=0.1)
    try:
        handle = source.schedule(tmp_path, _Recorder())
        assert handle is not None
    finally:
        source.stop()


def test_schedule_returns_none_when_file_discovery_construction_raises(
    tmp_path: Path,
) -> None:
    """Defense-in-depth beyond the ignore-spec fix: schedule()'s whole
    contract is "never crash the caller, return None instead" -- if
    building a FileDiscovery ever raises for a reason that layer's own
    contract does not cover, this boundary must still return None rather
    than propagate (djb/rev-silent finding).
    """
    from quarry.sync_discovery import FileDiscovery

    source = WatchdogSource(use_polling=True, poll_interval_s=0.1)
    try:
        with patch(
            "quarry.daemon.fs_watchdog.FileDiscovery",
            side_effect=RuntimeError("boom"),
        ):
            assert source.schedule(tmp_path, _Recorder()) is None
        # the source itself is unaffected -- a later, un-mocked call still works
        assert isinstance(FileDiscovery(tmp_path), FileDiscovery)
    finally:
        source.stop()


def test_schedule_returns_none_for_an_unresolvable_root(tmp_path: Path) -> None:
    """A registered root that does not exist is refused, never raises."""
    missing = tmp_path / "does-not-exist"
    source = WatchdogSource(use_polling=True, poll_interval_s=0.1)
    try:
        assert source.schedule(missing, _Recorder()) is None
    finally:
        source.stop()  # joins the observer thread under the bounded timeout


def test_build_observer_polling_overrides_pruned_choice() -> None:
    """use_polling=True always returns the PollingObserver, even when the
    pruned Linux observer is importable.
    """
    from watchdog.observers.polling import PollingObserver

    observer = WatchdogSource._build_observer(use_polling=True, poll_interval_s=0.1)
    assert isinstance(observer, PollingObserver)


def test_build_observer_picks_pruned_inotify_observer_when_importable() -> None:
    """A non-polling build picks the pruned inotify observer when it imported
    successfully at module scope (DES-045e) -- this suite runs on Linux, so
    the module-level import already succeeded; no per-test patching needed.
    """
    from quarry.daemon.inotify_prune_chain import PrunedInotifyObserver

    observer = WatchdogSource._build_observer(use_polling=False, poll_interval_s=2.0)
    try:
        assert isinstance(observer, PrunedInotifyObserver)
    finally:
        observer.stop()


def test_build_observer_falls_back_when_the_pruned_observer_is_unavailable() -> None:
    """A non-polling build falls back to watchdog's standard recursive observer
    when the module-scope import of the pruned Linux observer failed (or was
    patched away here to stand in for "off Linux") -- FSEvents on macOS is
    one stream per root with no per-directory kernel cost, so there is
    nothing to prune at this layer regardless of why the import is absent.
    """
    with patch.object(fs_watchdog, "_PrunedInotifyObserver", None):
        observer = WatchdogSource._build_observer(
            use_polling=False, poll_interval_s=2.0
        )
    try:
        assert type(observer).__module__ != "quarry.daemon.inotify_prune_chain"
    finally:
        observer.stop()


def test_module_load_logs_an_error_when_the_linux_import_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A Linux host whose pruned-observer import fails logs loudly and falls
    back, rather than silently reverting to unpruned recursive watching --
    djb's major finding: an `except (ImportError, UnsupportedLibcError)`
    with no platform check would make "watchdog upgrade broke us" on Linux
    indistinguishable from the expected, silent macOS fallback.

    ``sys.modules[name] = None`` is the standard technique for forcing an
    ``ImportError`` on the next ``from name import ...`` (Python's import
    system treats a ``None`` entry as "explicitly disallowed"), so this
    drives the REAL module-scope ``try``/``except`` in ``fs_watchdog.py`` via
    ``importlib.reload`` rather than asserting on a mock.
    """
    monkeypatch.setitem(sys.modules, "quarry.daemon.inotify_prune_chain", None)
    try:
        with caplog.at_level(logging.ERROR, logger="quarry.daemon.fs_watchdog"):
            importlib.reload(fs_watchdog)
        # vars()[...], not a direct attribute expression: pyright's strict
        # mode flags fs_watchdog._PrunedInotifyObserver as
        # reportPrivateImportUsage even for attribute access (not just
        # `from ... import`), since the module has no __all__ declaring it
        # exported; ruff's B009 in turn flags the getattr() alternative.
        assert vars(fs_watchdog)["_PrunedInotifyObserver"] is None
        assert any(
            record.levelno == logging.ERROR and "unavailable" in record.message
            for record in caplog.records
        )
    finally:
        # Restore the real module in sys.modules BEFORE reloading again, so
        # every other test in this process sees the correctly-imported
        # pruned observer, not the forced-failure state this test set up.
        monkeypatch.undo()
        importlib.reload(fs_watchdog)
