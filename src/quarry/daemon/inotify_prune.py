"""Linux-only: prune watchdog's recursive inotify watches per the shared ignore spec.

DES-045e (supersedes DES-045d's per-directory scheduling — operator-ratified).
Per-directory ``observer.schedule(..., recursive=False)`` (DES-045d) is
structurally wrong on Linux: watchdog gives every ``ObservedWatch`` its OWN
emitter, and each :class:`~watchdog.observers.inotify.InotifyEmitter`
constructs its own :class:`~watchdog.observers.inotify_c.Inotify`, which calls
the libc ``inotify_init()`` — one kernel INSTANCE plus ~2 threads and 3 fds
per directory. ``fs.inotify.max_user_instances`` defaults to 128 (per-user,
shared with IDEs/editors), so per-directory scheduling exhausts it at ~120
directories and silently degrades the whole tree to scan-only — the operator
will not raise this sysctl.

The fix returns to ONE recursive watch per root — one emitter, one kernel
instance, one thread-pair, regardless of tree size — and instead prunes the
much larger `fs.inotify.max_user_watches` budget (65,536, per-descriptor, no
per-user cap) by teaching the underlying ``Inotify`` wrapper (this module) to
skip ignored directories in both places it walks a directory tree:

* :meth:`Inotify._add_dir_watch` — the initial recursive walk run once from
  ``Inotify.__init__``.
* :meth:`Inotify._add_watch` — the single-directory primitive vanilla
  watchdog's ``read_events()`` auto-add path (and its nested
  ``_recursive_simulate`` helper, which backfills synthetic create events for
  a newly-created subtree's pre-existing contents) calls for every directory
  it discovers, including ones added later.

The chain that wires :class:`PrunedInotify` into watchdog's
``InotifyBuffer``/``InotifyEmitter``/``BaseObserver`` construction pipeline
lives in the sibling module :mod:`~quarry.daemon.inotify_prune_chain` — kept
separate so this module stays two classes: the pruning ALGORITHM, not the
plumbing that wires it in. ``test_inotify_prune.py``'s
``test_pins_the_watchdog_internals_this_module_subclasses`` fails loudly if a
future watchdog release renames or removes any of the methods either module
depends on, rather than the pruning silently going inert.

Platform scope: this module is imported ONLY on Linux
(:class:`~quarry.daemon.fs_watchdog.WatchdogSource` selects it by
``platform.system()``). macOS's FSEvents is one stream per root with NO
per-directory kernel cost, so it keeps watchdog's standard recursive
scheduling — pruning happens only at the post-debounce event filter
(:class:`~quarry.daemon.watch_submit.WatchSubmitter`), same as before DES-045d.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
from pathlib import Path
from typing import Self, final

from watchdog.observers.inotify_c import Inotify

from quarry.sync_discovery import FileDiscovery

logger = logging.getLogger(__name__)

# A watch descriptor the kernel never returns (real wd's are always >= 0 on
# success). Recorded in Inotify._wd_for_path for a pruned directory so a
# lookup by ANY caller inside vanilla watchdog's _recursive_simulate (which
# has no exception handling around its file-loop's parent-wd lookup) resolves
# instead of raising KeyError — see PrunedInotify._add_watch.
_PRUNED_WD = -2


class PrunedDirectoryError(OSError):
    """Raised by :meth:`PrunedInotify._add_watch` for a directory the shared
    ignore spec prunes -- a plain ``OSError`` subclass so every existing
    watchdog call site that already tolerates a per-directory ``OSError``
    (the auto-add path's ``except OSError: continue``, ``_recursive_simulate``'s
    ``contextlib.suppress(OSError)``) tolerates this with zero changes there.
    """

    __slots__ = ()


@final
class PrunedInotify(Inotify):
    """An ``Inotify`` wrapper whose recursive watch skips ignored directories.

    Builds one :class:`~quarry.sync_discovery.FileDiscovery` for the watched
    root at construction and consults it for every directory the initial walk
    or a later auto-add considers -- the SAME seam
    :meth:`~quarry.sync_discovery.FileDiscovery.discover` uses for a bulk
    scan, so a live watch and a scan can never disagree about what is
    ignored.

    Defines ``__new__``, not ``__init__`` (PY-CC-1): ``Inotify.__init__``'s
    behavior is exactly what this class needs to run, unmodified, AFTER
    ``self._root``/``self._discovery`` exist -- so rather than overriding
    ``__init__`` to call ``super().__init__(...)`` explicitly, ``__new__``
    sets those two attributes and returns; Python's normal construction
    protocol (``type.__call__``) then invokes the INHERITED
    ``Inotify.__init__`` automatically with the same arguments, which is
    exactly when ``_add_dir_watch`` (overridden below) first needs
    ``self._discovery`` to exist.
    """

    _root: Path
    _discovery: FileDiscovery

    def __new__(
        cls, path: bytes, *, recursive: bool = False, event_mask: int | None = None
    ) -> Self:
        # recursive/event_mask are unused here but MUST stay in the signature:
        # type.__call__ passes the constructor's args to both __new__ and the
        # automatically-invoked inherited __init__ (see the class docstring).
        del recursive, event_mask
        self = super().__new__(cls)
        self._root = Path(os.fsdecode(path))
        self._discovery = FileDiscovery(self._root)
        return self

    def _add_dir_watch(self, path: bytes, mask: int, *, recursive: bool) -> None:
        """Watch *path* and, if *recursive*, every non-ignored descendant.

        A verbatim-shaped rewrite of the vanilla walk with two differences:
        ``dirnames`` is pruned in place per the shared ignore spec (so a huge
        ignored subtree — a real ``node_modules`` — is never even descended
        into, unlike the auto-add path below, which cannot prune its walk —
        see :meth:`_add_watch`), and a per-directory ``OSError`` from
        ``_add_watch`` (ordinary churn: a directory vanishing between
        ``os.walk``'s readdir and this call) is logged and skipped rather
        than aborting the whole tree; only genuine budget exhaustion
        (``ENOSPC``/``EMFILE``) re-raises and aborts.
        """
        root = Path(os.fsdecode(path))
        if not root.is_dir():
            raise OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), path)
        self._add_watch(path, mask)  # the root itself: never pruned by construction
        if not recursive:
            return
        for dirpath_str, dirnames, _ in os.walk(root):
            dirpath = Path(dirpath_str)
            kept: list[str] = []
            for dirname in dirnames:
                full_path = dirpath / dirname
                if full_path.is_symlink():
                    continue
                if not self._is_watchable_or_fail_open(full_path):
                    continue
                kept.append(dirname)
                self._try_add_watch(os.fsencode(full_path), mask)
            dirnames[:] = kept

    def _try_add_watch(self, full_path: bytes, mask: int) -> None:
        """Add one vetted directory's watch; abort only on budget exhaustion."""
        try:
            self._add_watch(full_path, mask)
        except OSError as exc:
            if exc.errno in {errno.ENOSPC, errno.EMFILE}:
                # Release every fd/watch this partially-constructed instance
                # holds before aborting (Copilot, PR #503 round 2 -- the
                # earlier "documented 3-fd bound" undercounted: every REAL
                # watch descriptor already acquired by this walk lives until
                # the inotify fd closes, so a failed schedule was permanently
                # consuming watch budget and starving other registrations,
                # the quarry-ndrj leak shape relocated here).
                self._release_on_abort()
                raise
            logger.warning("watch: skipping %s during initial walk: %s", full_path, exc)

    def _release_on_abort(self) -> None:
        """Close every fd this partially-constructed instance holds.

        Vanilla ``Inotify.close()`` assumes an async reader thread exists
        to finish the close -- it only writes to the KILL pipe when
        ``self._is_reading`` is True, which it always is here (construction
        aborts before any thread ever starts, so nothing would read that
        byte and call ``_close_resources``). This bypasses that
        thread-signaling protocol and closes each fd directly: the inotify
        fd first (closing it releases every kernel watch this walk already
        acquired, the actual budget being freed here — not just the fd
        itself), then both kill-pipe ends, each independently
        suppress-protected so one failing close cannot leave the others
        open. Called from :meth:`_try_add_watch`'s ENOSPC/EMFILE abort path
        only, so the caller re-raises the ORIGINAL error afterward — a
        close failure here is swallowed, never substituted for it.
        """
        self._closed = True
        for fd in (self._inotify_fd, self._kill_r, self._kill_w):
            with contextlib.suppress(OSError):
                os.close(fd)

    def _add_watch(self, path: bytes, mask: int) -> int:
        """Add a real inotify watch for *path*, or refuse it if pruned.

        Every caller in vanilla watchdog that reaches this method -- the
        initial walk above (pre-filtered, so this branch is dead weight
        there but harmless), ``read_events()``'s auto-add for a newly created
        directory, and ``_recursive_simulate``'s per-subdirectory loop for a
        directory that arrives already populated -- already tolerates an
        ``OSError`` here (see :class:`PrunedDirectoryError`). The
        ``_wd_for_path`` sentinel write happens BEFORE the raise because
        ``_recursive_simulate``'s file loop looks up its parent directory's
        watch descriptor with no exception handling at all; without an
        entry, a file directly inside a pruned directory (found by that
        loop's own unprunable ``os.walk`` -- see the module docstring) would
        raise ``KeyError`` and crash the observer thread instead of just
        being silently unwatched. The bookkeeping this leaves behind for a
        pruned path is deliberately never cleaned up (the kernel never emits
        an ``IN_IGNORED`` for a watch that was never really installed, which
        is the only signal vanilla watchdog cleans up on) -- a bounded,
        small, per-process-lifetime Python dict entry per pruned path ever
        seen from a live create event, traded for never allocating a real
        kernel watch for it. This is the accepted cost of pruning a walk this
        method cannot itself prune (module docstring).

        Renaming a pruned directory (vanilla ``read_events()``'s
        ``is_moved_to`` branch) reuses the shared ``_PRUNED_WD`` sentinel at
        the new path and can overwrite ``_path_for_wd[_PRUNED_WD]`` if two
        DIFFERENT pruned directories are each renamed -- harmless by
        construction: the real kernel never emits an event whose ``wd``
        field equals ``_PRUNED_WD`` (no ``inotify_add_watch`` call can ever
        return a negative descriptor), so that reverse-lookup slot is never
        read through a real event; see
        ``test_read_events_renaming_a_pruned_directory_does_not_corrupt_state``
        in ``tests/test_inotify_prune.py``.
        """
        decoded = Path(os.fsdecode(path))
        if decoded != self._root and not self._is_watchable_or_fail_open(decoded):
            self._wd_for_path[path] = _PRUNED_WD
            msg = f"directory pruned by the shared ignore spec: {decoded}"
            raise PrunedDirectoryError(msg)
        return super()._add_watch(path, mask)

    def _is_watchable_or_fail_open(self, decoded: Path) -> bool:
        """Evaluate the ignore spec for *decoded*; treat "cannot tell" as watchable.

        This method runs on the watchdog buffer thread, which has no reader
        to recover it: an unhandled exception here kills the thread
        PERMANENTLY while :meth:`~quarry.daemon.watch_loop.WatchLoop.
        watch_state` keeps reporting "watched" (djb/rev-silent finding). The
        ignore-spec layer is written to never raise for malformed ignore
        FILES (:meth:`~quarry.ignore_spec.IgnoreRules._compile_lines`
        already skips invalid lines), so this is defense-in-depth against
        anything that layer's contract does not cover -- a future pathspec
        release, an unreadable filesystem object under a raced path, etc.
        Failing OPEN (watch it) is deliberate: an over-broad watch costs a
        few extra descriptors from the large ``max_user_watches`` budget;
        losing the buffer thread costs every watch on the whole tree.
        """
        try:
            return self._discovery.is_watchable_dir(decoded)
        except Exception:
            logger.exception(
                "watch: ignore-spec evaluation failed for %s; watching it "
                "rather than risking the buffer thread",
                decoded,
            )
            return True
