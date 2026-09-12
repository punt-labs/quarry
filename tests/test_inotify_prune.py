"""Tests for :class:`~quarry.daemon.inotify_prune.PrunedInotify` (DES-045e).

Drives ``PrunedInotify`` directly against FAKED ``inotify_init``/
``inotify_add_watch``/``inotify_rm_watch`` ctypes calls (module-level
functions in ``watchdog.observers.inotify_c``) so the pruning DECISION
logic — which directories get a real (fake) watch, which get silently
skipped, and how a per-directory failure is handled — is covered
deterministically, independent of the host's actual inotify instance/watch
headroom (a real desktop session routinely has near-zero
``fs.inotify.max_user_instances`` headroom shared with IDEs and other
processes, which is the exact scarcity this fix exists for). The watchdog
construction-chain wiring (``PrunedInotifyBuffer``/``PrunedInotifyEmitter``/
``PrunedInotifyObserver``, including the real-kernel end-to-end slow test)
lives in ``tests/test_inotify_prune_chain.py``.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import itertools
import os
import struct
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import watchdog.observers.inotify_c as inotify_c
from watchdog.observers.inotify_c import InotifyConstants

from quarry.daemon.inotify_prune import PrunedDirectoryError, PrunedInotify
from quarry.sync_discovery import FileDiscovery

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="inotify pruning is Linux-only"
)

_PRUNED_WD = -2


class _FakeKernel:
    """Fake ``inotify_init``/``inotify_add_watch``/``inotify_rm_watch``.

    ``init()`` hands out the read end of a REAL ``os.pipe()`` rather than a
    fabricated integer: a fabricated fd leaks into ``select.poll()``/
    ``os.read()`` (both real syscalls -- only the three ctypes entry points
    are faked) as an int that happens not to be open, and ``Inotify.close()``
    ->``_close_resources()`` calling ``os.close()`` on a fabricated fd raises
    ``OSError: Bad file descriptor`` for any test that reaches that path
    (:meth:`Inotify.read_events` called directly, no background reader
    thread). A real pipe end tolerates every one of those calls correctly.
    ``close()`` closes every fd this kernel ever handed out, for tests whose
    ``Inotify`` never reaches ``_close_resources()`` itself (never calls
    ``read_events()``, so ``Inotify.close()`` only writes to the KILL pipe
    for a background reader thread that, in these synchronous tests, does
    not exist to read it and finish the close) -- otherwise every such fd
    leaks for the life of the test process. ``fail_paths`` maps one encoded
    path to the errno ``inotify_add_watch`` should fail with for that path
    only.
    """

    def __init__(self, *, fail_paths: dict[bytes, int] | None = None) -> None:
        self._wds = itertools.count(1)
        self._fail_paths = fail_paths or {}
        self._open_fds: list[int] = []

    def init(self) -> int:
        read_fd, write_fd = os.pipe()
        self._open_fds.extend((read_fd, write_fd))
        return read_fd

    def add_watch(self, fd: int, path: bytes, mask: int) -> int:
        del fd, mask
        if path in self._fail_paths:
            ctypes.set_errno(self._fail_paths[path])
            return -1
        return next(self._wds)

    @staticmethod
    def rm_watch(fd: int, wd: int) -> int:
        del fd, wd
        return 0

    def close(self) -> None:
        for fd in self._open_fds:
            with contextlib.suppress(OSError):
                os.close(fd)
        self._open_fds.clear()


@pytest.fixture
def fake_kernel() -> Iterator[_FakeKernel]:
    """Patch the module-level ctypes calls ``Inotify`` invokes, for one test."""
    kernel = _FakeKernel()
    with (
        patch.object(inotify_c, "inotify_init", kernel.init),
        patch.object(inotify_c, "inotify_add_watch", kernel.add_watch),
        patch.object(inotify_c, "inotify_rm_watch", kernel.rm_watch),
    ):
        yield kernel
    kernel.close()


def _close_inotify(inst: PrunedInotify) -> None:
    """Close *inst*, then force-close its KILL pipe.

    ``Inotify.close()`` only writes a byte to the kill pipe to wake a
    BACKGROUND reader thread blocked in ``read_events()`` -- that thread is
    what actually closes ``_kill_r``/``_kill_w`` (inside
    ``_close_resources``, once it notices ``self._closed``). These tests
    construct ``PrunedInotify`` directly and never run that reader thread,
    so the write sits unread and the pipe leaks for the life of the test
    process unless closed here explicitly. A test that DID call
    ``read_events()`` directly (see
    ``test_read_events_survives_a_deep_create_burst_under_a_pruned_directory``)
    already closed both ends via ``_close_resources``, so ``OSError`` here
    is expected and tolerated, not a bug.
    """
    inst.close()
    for fd in (inst._kill_r, inst._kill_w):
        with contextlib.suppress(OSError):
            os.close(fd)


def _watched_paths(inst: PrunedInotify) -> set[str]:
    return {p.decode() for p, wd in inst._wd_for_path.items() if wd != _PRUNED_WD}


def _pruned_sentinel_paths(inst: PrunedInotify) -> set[str]:
    return {p.decode() for p, wd in inst._wd_for_path.items() if wd == _PRUNED_WD}


def test_pins_the_watchdog_internals_this_module_subclasses() -> None:
    """A loud, fast check that watchdog's private inotify internals this
    module overrides still have the shape it was coded against.

    If a future watchdog release renames or removes one of these, this
    fails immediately and explicitly instead of the pruning silently going
    inert (the whole point of "pin the assumption with a test").
    """
    import inspect

    from watchdog.observers.inotify_c import Inotify

    assert "_add_dir_watch" in vars(Inotify)
    add_dir_params = set(inspect.signature(Inotify._add_dir_watch).parameters)
    assert {"self", "path", "mask", "recursive"} <= add_dir_params

    assert "_add_watch" in vars(Inotify)
    add_watch_params = set(inspect.signature(Inotify._add_watch).parameters)
    assert {"self", "path", "mask"} <= add_watch_params


def test_add_dir_watch_prunes_ignored_directories(
    tmp_path: Path, fake_kernel: _FakeKernel
) -> None:
    """The initial recursive walk never adds (or even attempts) a watch for
    an ignored directory -- confirmed by BOTH the watched set and the
    absence of even a pruned-sentinel entry, since the pre-filtered walk
    never calls ``_add_watch`` for it at all.
    """
    del fake_kernel
    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules" / "pkg" / "deep").mkdir(parents=True)
    (tmp_path / ".git").mkdir()

    inst = PrunedInotify(str(tmp_path).encode(), recursive=True, event_mask=None)
    try:
        assert _watched_paths(inst) == {str(tmp_path), str(tmp_path / "src")}
        assert _pruned_sentinel_paths(inst) == set()
    finally:
        _close_inotify(inst)


def test_add_dir_watch_empty_directory_watches_only_the_root(
    tmp_path: Path, fake_kernel: _FakeKernel
) -> None:
    """Boundary: a root with no subdirectories yields exactly one watch."""
    del fake_kernel
    inst = PrunedInotify(str(tmp_path).encode(), recursive=True, event_mask=None)
    try:
        assert _watched_paths(inst) == {str(tmp_path)}
    finally:
        _close_inotify(inst)


def test_add_dir_watch_bounded_regardless_of_ignored_subtree_size(
    tmp_path: Path, fake_kernel: _FakeKernel
) -> None:
    """A large ignored subtree contributes zero watches -- the actual bug."""
    del fake_kernel
    for i in range(150):
        (tmp_path / "node_modules" / f"pkg{i}").mkdir(parents=True)
    (tmp_path / "src").mkdir()

    inst = PrunedInotify(str(tmp_path).encode(), recursive=True, event_mask=None)
    try:
        assert _watched_paths(inst) == {str(tmp_path), str(tmp_path / "src")}
    finally:
        _close_inotify(inst)


def test_add_dir_watch_skips_ordinary_oserror_and_continues(tmp_path: Path) -> None:
    """A single directory's ordinary add-watch failure (raced ENOENT) is
    skipped -- siblings still get watched, the tree is not aborted.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "c").mkdir()
    fail_b = {str(tmp_path / "b").encode(): errno.ENOENT}
    kernel = _FakeKernel(fail_paths=fail_b)
    with (
        patch.object(inotify_c, "inotify_init", kernel.init),
        patch.object(inotify_c, "inotify_add_watch", kernel.add_watch),
        patch.object(inotify_c, "inotify_rm_watch", kernel.rm_watch),
    ):
        inst = PrunedInotify(str(tmp_path).encode(), recursive=True, event_mask=None)
        try:
            watched = _watched_paths(inst)
            assert str(tmp_path / "a") in watched
            assert str(tmp_path / "c") in watched
            assert str(tmp_path / "b") not in watched
        finally:
            _close_inotify(inst)


@pytest.mark.parametrize("bad_errno", [errno.ENOSPC, errno.EMFILE])
def test_add_dir_watch_aborts_on_budget_exhaustion(
    tmp_path: Path, bad_errno: int
) -> None:
    """ENOSPC/EMFILE abort the whole initial walk -- these mean the budget
    is genuinely exhausted, not a transient per-directory race.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    fail_b = {str(tmp_path / "b").encode(): bad_errno}
    kernel = _FakeKernel(fail_paths=fail_b)
    with (
        patch.object(inotify_c, "inotify_init", kernel.init),
        patch.object(inotify_c, "inotify_add_watch", kernel.add_watch),
        patch.object(inotify_c, "inotify_rm_watch", kernel.rm_watch),
        pytest.raises(OSError, match=r"limit reached"),
    ):
        PrunedInotify(str(tmp_path).encode(), recursive=True, event_mask=None)


@pytest.mark.parametrize("bad_errno", [errno.ENOSPC, errno.EMFILE])
def test_add_dir_watch_releases_resources_on_budget_exhaustion(
    tmp_path: Path, bad_errno: int
) -> None:
    """A budget-exhaustion abort must not leak the inotify fd or the kill
    pipe (Copilot, PR #503 round 2). Closing the inotify fd is the real
    budget relief: on Linux, closing an inotify instance's fd releases
    every watch descriptor already acquired on it -- the earlier
    "documented 3-fd bound" undercounted the cost, since a failed
    schedule was leaving every watch this walk had ALREADY acquired
    permanently consuming budget (the quarry-ndrj leak shape relocated).

    Constructs via ``__new__`` + a direct ``Inotify.__init__`` call
    (rather than ``PrunedInotify(...)``) so the partially-constructed
    instance is still reachable after the raise -- ``PrunedInotify(...)``
    itself would lose the only reference to it the moment the exception
    propagates out of construction.
    """
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    fail_b = {str(tmp_path / "b").encode(): bad_errno}
    kernel = _FakeKernel(fail_paths=fail_b)
    with (
        patch.object(inotify_c, "inotify_init", kernel.init),
        patch.object(inotify_c, "inotify_add_watch", kernel.add_watch),
        patch.object(inotify_c, "inotify_rm_watch", kernel.rm_watch),
    ):
        path = str(tmp_path).encode()
        inst = PrunedInotify.__new__(
            PrunedInotify, path, recursive=True, event_mask=None
        )
        with pytest.raises(OSError, match=r"limit reached"):
            inotify_c.Inotify.__init__(inst, path, recursive=True, event_mask=None)

    assert inst._closed is True
    for fd in (inst._inotify_fd, inst._kill_r, inst._kill_w):
        with pytest.raises(OSError):  # closing an already-closed fd raises EBADF
            os.close(fd)


def test_add_watch_rejects_an_ignored_path_and_registers_the_sentinel(
    tmp_path: Path, fake_kernel: _FakeKernel
) -> None:
    """A direct ``_add_watch`` call for an ignored path raises
    ``PrunedDirectoryError`` (a plain ``OSError`` subclass every existing
    watchdog call site already tolerates) and leaves a sentinel entry so a
    later parent-wd lookup (``_recursive_simulate``'s file loop) resolves
    instead of raising ``KeyError``.
    """
    del fake_kernel
    (tmp_path / "node_modules").mkdir()
    inst = PrunedInotify(str(tmp_path).encode(), recursive=False, event_mask=None)
    try:
        ignored = str(tmp_path / "node_modules").encode()
        with pytest.raises(PrunedDirectoryError):
            inst._add_watch(ignored, 0)
        assert inst._wd_for_path[ignored] == _PRUNED_WD
    finally:
        _close_inotify(inst)


def test_add_watch_never_prunes_the_root_itself(
    tmp_path: Path, fake_kernel: _FakeKernel
) -> None:
    """The registered root is always watched, even though ``is_watchable_dir``
    would reject it if it were (incorrectly) checked against itself.
    """
    del fake_kernel
    inst = PrunedInotify(str(tmp_path).encode(), recursive=False, event_mask=None)
    try:
        assert str(tmp_path) in _watched_paths(inst)
    finally:
        _close_inotify(inst)


def test_read_events_survives_a_deep_create_burst_under_a_pruned_directory(
    tmp_path: Path, fake_kernel: _FakeKernel
) -> None:
    """A deterministic, default-tier pin for the crash guard the slow
    end-to-end test (``tests/test_inotify_prune_chain.py``) exercises
    against a real kernel: one crafted raw ``inotify_event`` struct
    (``struct.pack("iIII", wd, IN_CREATE | IN_ISDIR, 0, len(name)) +
    name``, the exact layout ``Inotify._parse_event_buffer`` decodes) for a
    single "burst" directory-create, patched in via ``os.read`` and
    ``_check_inotify_fd`` so ``read_events()`` runs its real auto-add and
    ``_recursive_simulate`` logic against burst's disk state -- already
    containing BOTH a watchable file and an ignored one by the time this
    synthetic event is processed, the exact race the sentinel bookkeeping
    in :meth:`PrunedInotify._add_watch` exists for (a missing entry would
    raise ``KeyError`` from ``_recursive_simulate``'s unprotected
    parent-wd lookup and kill the observer thread). Fake-kernel-backed and
    single-threaded (``read_events()`` called directly, no observer
    thread), so this needs no real inotify headroom and runs in the
    default (non-slow) tier.
    """
    del fake_kernel
    burst = tmp_path / "burst"
    (burst / "a").mkdir(parents=True)
    (burst / "a" / "deep.txt").write_text("deep")
    (burst / "node_modules" / "pkg").mkdir(parents=True)
    (burst / "node_modules" / "pkg" / "ignored.txt").write_text("ignored")

    inst = PrunedInotify(str(tmp_path).encode(), recursive=True, event_mask=None)
    try:
        root_wd = inst._wd_for_path[str(tmp_path).encode()]
        name = b"burst"
        mask = InotifyConstants.IN_CREATE | InotifyConstants.IN_ISDIR
        raw_event = struct.pack("iIII", root_wd, mask, 0, len(name)) + name

        inst._check_inotify_fd = lambda: True  # skip the real select.poll()
        with patch.object(os, "read", return_value=raw_event):
            events = inst.read_events()  # must not raise KeyError

        deep_file = os.fsencode(burst / "a" / "deep.txt")
        assert any(e.src_path == deep_file for e in events), (
            "the deep watchable file's backfilled create event is missing"
        )

        pruned_prefix = str(burst / "node_modules") + "/"
        real_watches_below_pruned = {
            path.decode(): wd
            for path, wd in inst._wd_for_path.items()
            if path.decode().startswith(pruned_prefix) and wd != _PRUNED_WD
        }
        assert not real_watches_below_pruned, (
            f"a real watch was installed below the pruned directory: "
            f"{real_watches_below_pruned}"
        )
    finally:
        _close_inotify(inst)


def test_read_events_survives_a_deep_create_burst_under_a_nested_gitignore(
    tmp_path: Path, fake_kernel: _FakeKernel
) -> None:
    """Copilot finding, PR #503: a burst-created directory two levels below
    a NESTED (not root) ``.gitignore`` match must not get a real watch.

    ``x/.gitignore`` names ``logs/``; a burst creates ``x/logs/deep``
    already populated. The live auto-add event for "logs" (a direct child
    of the already-watched ``x``) must be rejected by consulting ``x``'s
    own local spec during the ancestor walk -- checking only ``x/logs``'s
    immediate parent (``x/logs`` itself, which has no ``.gitignore``) is
    not enough; ``_recursive_simulate`` is never reached for "logs" once
    ``_add_watch`` rejects it, so "deep" gets no watch either.
    """
    del fake_kernel
    x = tmp_path / "x"
    x.mkdir()
    inst = PrunedInotify(str(tmp_path).encode(), recursive=True, event_mask=None)
    try:
        (x / ".gitignore").write_text("logs/\n")
        (x / "logs" / "deep").mkdir(parents=True)

        x_wd = inst._wd_for_path[os.fsencode(x)]
        name = b"logs"
        mask = InotifyConstants.IN_CREATE | InotifyConstants.IN_ISDIR
        raw_event = struct.pack("iIII", x_wd, mask, 0, len(name)) + name

        inst._check_inotify_fd = lambda: True  # skip the real select.poll()
        with patch.object(os, "read", return_value=raw_event):
            inst.read_events()  # must not raise

        pruned_prefix = str(x / "logs") + "/"
        real_watches_below_pruned = {
            path.decode(): wd
            for path, wd in inst._wd_for_path.items()
            if path.decode().startswith(pruned_prefix) and wd != _PRUNED_WD
        }
        assert not real_watches_below_pruned, (
            f"a real watch was installed below the nested-gitignore-matched "
            f"directory: {real_watches_below_pruned}"
        )
    finally:
        _close_inotify(inst)


def test_read_events_renaming_a_pruned_directory_does_not_corrupt_state(
    tmp_path: Path, fake_kernel: _FakeKernel
) -> None:
    """Renaming a pruned directory reuses the shared ``_PRUNED_WD`` sentinel
    at its new path (djb minor) -- vanilla watchdog's move handling
    (``read_events``'s ``is_moved_to`` branch) does not distinguish a real
    wd from the sentinel: it deletes the old path's ``_wd_for_path`` entry,
    inserts the new path with the SAME value, and points
    ``_path_for_wd[value]`` at the new path. For two pruned directories
    with DIFFERENT original paths, this makes ``_path_for_wd[_PRUNED_WD]``
    collide (whichever was renamed most recently wins that slot) -- but
    this is harmless BY CONSTRUCTION: the real kernel never emits an event
    whose ``wd`` field is ``_PRUNED_WD`` (a negative int no
    ``inotify_add_watch`` call can ever return), so
    ``wd_path = self._path_for_wd[wd]`` in ``read_events`` can never
    resolve through the sentinel slot -- only ``_wd_for_path`` (keyed by
    PATH, never colliding) is ever read for a pruned entry. This test pins
    that no exception is raised and both paths' ``_wd_for_path``
    bookkeeping stays internally consistent across the rename.
    """
    del fake_kernel
    (tmp_path / "node_modules").mkdir()

    inst = PrunedInotify(str(tmp_path).encode(), recursive=True, event_mask=None)
    try:
        old_path = os.fsencode(tmp_path / "node_modules")
        new_path = os.fsencode(tmp_path / "renamed_prunable")
        with pytest.raises(PrunedDirectoryError):
            inst._add_watch(old_path, 0)
        assert inst._wd_for_path[old_path] == _PRUNED_WD

        root_wd = inst._wd_for_path[os.fsencode(tmp_path)]
        cookie = 42
        old_name, new_name = b"node_modules", b"renamed_prunable"
        moved_from = (
            struct.pack(
                "iIII",
                root_wd,
                InotifyConstants.IN_MOVED_FROM | InotifyConstants.IN_ISDIR,
                cookie,
                len(old_name),
            )
            + old_name
        )
        moved_to = (
            struct.pack(
                "iIII",
                root_wd,
                InotifyConstants.IN_MOVED_TO | InotifyConstants.IN_ISDIR,
                cookie,
                len(new_name),
            )
            + new_name
        )

        inst._check_inotify_fd = lambda: True  # skip the real select.poll()
        with patch.object(os, "read", return_value=moved_from + moved_to):
            inst.read_events()  # must not raise

        assert old_path not in inst._wd_for_path
        assert inst._wd_for_path[new_path] == _PRUNED_WD
    finally:
        _close_inotify(inst)


def test_construction_survives_a_malformed_root_gitignore(
    tmp_path: Path, fake_kernel: _FakeKernel
) -> None:
    """A malformed root .gitignore must not crash PrunedInotify construction
    (djb/rev-silent finding, daemon-start path) -- ignore_spec.IgnoreRules
    now compiles line-tolerantly, so this closes at the source rather than
    needing a try/except wrapper at every construction site.
    """
    del fake_kernel
    (tmp_path / ".gitignore").write_text("*.log\n!\ndata/\n")
    (tmp_path / "src").mkdir()
    inst = PrunedInotify(str(tmp_path).encode(), recursive=True, event_mask=None)
    try:
        assert str(tmp_path / "src") in _watched_paths(inst)
    finally:
        _close_inotify(inst)


def test_initial_walk_survives_a_malformed_nested_gitignore(
    tmp_path: Path, fake_kernel: _FakeKernel
) -> None:
    """A malformed NESTED .gitignore, compiled lazily while the INITIAL walk
    decides whether to watch a child directory, must not abort construction
    -- the exact 'mid-session nested bad .gitignore' scenario (djb/
    rev-silent), but hit here at construction time rather than via a later
    live create event.
    """
    del fake_kernel
    sub = tmp_path / "sub"
    (sub / "child").mkdir(parents=True)
    (sub / ".gitignore").write_text("*.tmp\n!\nkeep_out/\n")
    inst = PrunedInotify(str(tmp_path).encode(), recursive=True, event_mask=None)
    try:
        assert str(sub / "child") in _watched_paths(inst)
    finally:
        _close_inotify(inst)


def test_read_events_survives_a_malformed_nested_gitignore_written_mid_session(
    tmp_path: Path, fake_kernel: _FakeKernel
) -> None:
    """The exact mid-session crash site (djb/rev-silent finding): a nested
    .gitignore written AFTER construction, compiled lazily by
    ``_add_watch``'s live auto-add path when a new child directory arrives
    -- NOT the initial-walk path :func:`test_initial_walk_survives_a_
    malformed_nested_gitignore` covers. Without the ignore-spec fix this
    kills the watchdog buffer thread permanently (no reader recovers a
    raised exception there) while ``watch_state`` keeps reporting
    "watched"; ``read_events()`` is the exact call a real buffer thread's
    run loop makes, so this pins the SAME code path deterministically.
    """
    del fake_kernel
    sub = tmp_path / "sub"
    sub.mkdir()
    inst = PrunedInotify(str(tmp_path).encode(), recursive=True, event_mask=None)
    try:
        sub_wd = inst._wd_for_path[os.fsencode(sub)]

        # Mid-session: a bad nested .gitignore lands alongside a live create.
        (sub / ".gitignore").write_text("*.tmp\n!\nkeep_out/\n")
        (sub / "child").mkdir()

        name = b"child"
        mask = InotifyConstants.IN_CREATE | InotifyConstants.IN_ISDIR
        raw_event = struct.pack("iIII", sub_wd, mask, 0, len(name)) + name

        inst._check_inotify_fd = lambda: True  # skip the real select.poll()
        with patch.object(os, "read", return_value=raw_event):
            inst.read_events()  # must not raise
    finally:
        _close_inotify(inst)


def test_add_watch_fails_open_when_the_ignore_spec_raises_unexpectedly(
    tmp_path: Path, fake_kernel: _FakeKernel
) -> None:
    """Defense-in-depth beyond the ignore-spec compile fix: if
    FileDiscovery.is_watchable_dir ever raises for a reason THAT layer's
    own contract does not cover, _add_watch must fail OPEN (watch it)
    rather than let the exception kill the watchdog buffer thread
    permanently while watch_state keeps reporting "watched".
    """
    del fake_kernel
    (tmp_path / "sub").mkdir()
    inst = PrunedInotify(str(tmp_path).encode(), recursive=False, event_mask=None)
    try:
        with patch.object(
            FileDiscovery, "is_watchable_dir", side_effect=RuntimeError("boom")
        ):
            wd = inst._add_watch(os.fsencode(tmp_path / "sub"), 0)
        assert wd != _PRUNED_WD
        assert str(tmp_path / "sub") in _watched_paths(inst)
    finally:
        _close_inotify(inst)
