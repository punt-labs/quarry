"""The single-flight guard around ``quarry sync``'s detached background launch."""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Self, final

logger = logging.getLogger(__name__)


@final
class SyncLock:
    """Own the sync lockfile and arbitrate single-flight DISPATCH of ``quarry sync``.

    Scope, precisely: ``quarry sync`` is dispatch-only — it POSTs a sync
    request to the daemon and exits after the daemon's 202 acceptance, well
    before the daemon's scan itself finishes.  This lock's ``flock`` is held
    for exactly that dispatch subprocess's lifetime, so it prevents redundant
    or racing *launches* of the dispatcher — it does NOT, and is not meant to,
    span the daemon's actual scan.  The daemon does NOT deduplicate or
    coalesce scans either: per DES-045, the sync route always returns 202 and
    enqueues a ``CollectionSyncJob`` per registered collection on every
    request — there is no singleton-task 409 for sync (that guard exists only
    for ``optimize``/``backfill``, which have no per-collection queue).  What
    keeps concurrent scans from stepping on each other is DES-042's
    daemon-owned FIFO ingest queue, which serializes work per collection.  Do
    not try to make this flock span the scan or dedupe scan requests — that is
    the daemon's queue's job, and this class has no visibility into it.

    One instance, one lockfile path.  Mutual exclusion for the dispatch is a
    kernel ``flock(2)`` on that path, not a PID written into it: the lock is
    held for the dispatch subprocess's entire lifetime (the child inherits
    the parent's open file description via ``Popen(pass_fds=...)``) and is
    released automatically by the kernel when the child exits or dies —
    no PID parsing, no staleness detection, and therefore no reclaim race.
    A prior PID/``O_CREAT|O_EXCL`` design had exactly that race: two
    callers independently deciding the same stale PID was reclaimable
    could each ``unlink()`` and each recreate the lock, interleaving into
    a duplicate dispatch — ``flock`` has no equivalent "decide staleness,
    then act on it" step for a second caller to interleave with.

    The lockfile is never deleted BY THIS CLASS, but ``flock`` is bound to
    the inode, not the path: if the file is deleted EXTERNALLY while a
    dispatch holds it (an operator ``rm``, a future cleanup/doctor script), a
    subsequent :meth:`acquire` happily ``O_CREAT``\\ s a fresh inode at the
    same path, unrelated to the still-locked old one — silently defeating
    single-flight.  Do not "fix" a lockfile that looks stuck by removing it
    while a dispatch might be active; the flock, not the path, is the source
    of truth for whether one is running.
    """

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path | None = None) -> Self:
        self = super().__new__(cls)
        self._path = path if path is not None else cls._default_path()
        return self

    @staticmethod
    def _default_path() -> Path:
        """Return the default lockfile path in a user-owned directory."""
        return Path.home() / ".punt-labs" / "quarry" / "sync.pid"

    @property
    def path(self) -> Path:
        """Return the lockfile path this lock guards."""
        return self._path

    def _open_for_lock(self) -> int:
        """Open (creating if needed) the lockfile; caller owns the fd.

        The file is never deleted by this class — it is a permanent, shared
        scaffold for the kernel lock, reused across every session.
        ``O_NOFOLLOW`` refuses to follow a symlink planted at the fixed path:
        without it, a hostile or accidental ``sync.pid`` symlink would make
        this open (and the later ``ftruncate``/``write``) target whatever the
        symlink points at.  It only guards the final path component — the
        parent directory is the trusted, user-owned one — which is exactly
        the guarantee needed here.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(str(self._path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)

    def is_held(self) -> bool:
        """Return whether another process currently holds the sync lock.

        This is an ADVISORY, non-mutating probe — a momentary snapshot, not
        a reservation.  A ``False`` here does not guarantee a subsequent
        :meth:`acquire` will succeed (a concurrent acquirer could win
        first), and a ``True`` here can go stale the instant the holder
        exits.  The only trustworthy single-winner decision is
        :meth:`acquire`'s own ``flock(LOCK_EX | LOCK_NB)`` attempt; this
        method exists purely as a cheap fast-path skip.
        """
        try:
            fd = self._open_for_lock()
        except OSError as exc:
            logger.error("session-start: failed to open lock file for probe: %s", exc)
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Normal contention: another process holds it. Expected, not logged.
            return True
        except OSError as exc:
            # A genuine kernel error (e.g. ENOLCK, lock-table exhausted) --
            # never conflate this with routine contention, or it silently
            # masquerades as "someone else is syncing" forever with no trace.
            logger.error("session-start: flock probe failed: %s", exc)
            return True
        else:
            # Best-effort release: the finally-close below already releases
            # the flock (closing the last fd on the open file description
            # does), so a transient OSError unlocking explicitly here must
            # not escape and turn this advisory probe into a raise.
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def acquire(self) -> int | None:
        """Attempt to take the sync lock; return the held fd, or ``None``.

        ``flock(LOCK_EX | LOCK_NB)`` makes the kernel the single arbiter of
        "who won" — there is no check-then-act window for a second caller
        to exploit.  The returned fd is meant to be handed to the sync
        subprocess via ``Popen(pass_fds=(fd,))`` so the lock survives for
        the subprocess's whole lifetime.
        """
        try:
            fd = self._open_for_lock()
        except OSError as exc:
            logger.error("session-start: failed to open lock file: %s", exc)
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Normal contention: another process holds it. Expected, not logged.
            os.close(fd)
            return None
        except OSError as exc:
            # A genuine kernel error (e.g. ENOLCK) -- must be visible, not
            # silently treated as routine contention.
            logger.error("session-start: flock acquire failed: %s", exc)
            os.close(fd)
            return None
        return fd

    def launch_background_sync(self) -> str:
        """Fire-and-forget sync via detached subprocess.

        Uses ``sys.executable -m quarry`` to avoid PATH trust issues (the
        hook runs automatically on SessionStart with no user confirmation).
        Redirects all stdio to DEVNULL — especially stdin, to prevent the
        child from holding Claude Code's stdin pipe open after the parent
        exits.  The subprocess gets its own process group so it survives
        the hook process.

        The lock fd is marked inheritable and passed to the child via
        ``pass_fds`` so the child's copy of the SAME open file description
        keeps the ``flock`` held after this process closes its own copy —
        the lock persists for exactly the sync's lifetime, released by the
        kernel the instant the child exits or dies, live or crashed alike.

        Returns ``"launched"`` if the subprocess was started, ``"running"``
        if a sync is already in progress (or the lock is held), or
        ``"failed"`` if the launch itself errored.
        """
        # Fast path: if a sync is already running, skip without trying the lock.
        if self.is_held():
            logger.debug("session-start: sync already running, skipping")
            return "running"

        # The one genuine single-winner decision — see is_held()'s docstring.
        fd = self.acquire()
        if fd is None:
            logger.debug("session-start: could not acquire sync lock, skipping")
            return "running"

        # Survive execve(): pass_fds forces close_fds to skip this fd and
        # marks it inheritable for us, but clearing it explicitly here keeps
        # the exec-survival property visible and intentional in the code,
        # not an implicit side effect of subprocess's internals.
        inheritable = True
        os.set_inheritable(fd, inheritable)
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "quarry", "sync"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=(fd,),
            )
        except OSError as exc:
            logger.error("session-start: failed to launch background sync: %s", exc)
            # No child inherited a copy — closing ours fully releases the lock.
            os.close(fd)
            return "failed"

        # Record the PID for observability only — the flock the child
        # inherited, not this content, is now the actual lock.  A short
        # write is retried until the full PID lands, so a later human
        # inspecting the file never sees a truncated or stale value.
        try:
            os.ftruncate(fd, 0)
            data = str(proc.pid).encode()
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
        except OSError as exc:
            logger.warning(
                "session-start: sync launched but pidfile write failed: %s", exc
            )
        finally:
            # Close OUR copy — the child's inherited copy of the SAME open
            # file description keeps the flock held until it exits or dies.
            os.close(fd)

        logger.info("session-start: background sync launched (pid=%d)", proc.pid)
        return "launched"
