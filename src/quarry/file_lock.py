"""An exclusive advisory lock over a sibling lock file, for one host mutation."""

from __future__ import annotations

import contextlib
import fcntl
import os
import threading
from typing import TYPE_CHECKING, ClassVar, Self, final

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType

__all__ = ["FILE_LOCK_GITIGNORE_GLOB", "FileLock"]

# Matches every lock filename this class creates (".{target.name}.lock",
# e.g. ".CLAUDE.md.lock", "..gitignore.lock"). Exposed for quarry.gitignore
# to exclude, since a lock file is created and, by design, never removed --
# unignored, a bare `git add -A` commits a machine-local artifact.
FILE_LOCK_GITIGNORE_GLOB = ".*.lock"


@final
class FileLock:
    """Hold an exclusive ``flock`` on a sibling lock file for one read-modify-write.

    A shared host-file mutation in tool-enable-disable.md § 2.4 — the
    ``CLAUDE.md`` import line — is a read-modify-write on a file other tools and
    invocations also touch. Atomic rename prevents a torn file but not a lost
    update: two parallel ``enable`` runs each read the old bytes, write their
    change, and rename, and the second silently clobbers the first. This lock
    serializes them.

    The lock file lives beside its target (``.CLAUDE.md.punt-import.lock``
    next to ``CLAUDE.md``, via :meth:`shared`) so it shares the directory the atomic rename needs anyway,
    and is created — never removed — so a waiter and a holder always ``flock``
    the same inode.

    **Reentrant within a process.** A nested acquisition of the same lock path
    (``Enablement`` holds the lock across its whole enable/disable sequence while
    ``ClaudeMdImport.register`` re-acquires it for the import edit) shares the one
    ``flock`` rather than opening a second descriptor. ``flock`` treats two
    independent opens of one file as independent holders even inside a single
    process, so a naive nested acquire would deadlock against the outer hold;
    counting the depth and locking once avoids that. Cross-process serialization
    is unaffected — another process blocks on the real ``flock`` until this
    process releases its outermost acquisition. (The depth registry is keyed by
    path for bookkeeping only; the intended nesting is sequential within one
    thread.)
    """

    __slots__ = ("_lock_path",)

    _lock_path: Path

    # Process-wide reentrancy registry, keyed by the lock file's path. ``_guard``
    # serializes the bookkeeping only; the blocking ``flock`` is taken outside it
    # so a wait never stalls another path's acquisition.
    _guard: ClassVar[threading.Lock] = threading.Lock()
    _fd_by_path: ClassVar[dict[str, int]] = {}
    _depth_by_path: ClassVar[dict[str, int]] = {}

    def __new__(cls, target: Path) -> Self:
        self = super().__new__(cls)
        self._lock_path = target.parent / f".{target.name}.lock"
        return self

    @classmethod
    def shared(cls, target: Path) -> Self:
        """Lock a host file other punt tools also edit, on the mandated name.

        tool-enable-disable.md § 2.4 fixes the sibling ``.<name>.punt-import.lock``
        for ``CLAUDE.md`` and ``.claude/settings.json``: every tool must take the
        identical lock, or one serializes only against itself.
        """
        self = cls(target)
        self._lock_path = target.parent / f".{target.name}.punt-import.lock"
        return self

    def __enter__(self) -> Self:
        key = str(self._lock_path)
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock._guard:
            if FileLock._depth_by_path.get(key, 0) > 0:
                # Already held by this process: reenter, sharing the one flock.
                FileLock._depth_by_path[key] += 1
                return self
        # O_NOFOLLOW refuses a symlinked lock path: the parent is the trusted
        # resolved repo root, so a planted ``.CLAUDE.md.lock`` symlink is the
        # only escape, and following it would flock (and O_CREAT-create) a file
        # outside the repo. The resulting ELOOP propagates as the acquire's
        # refusal — a hostile lock symlink means refuse to enable/disable, which
        # is correct; a normal regular lock file is unaffected.
        fd = os.open(key, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o644)
        # A blocking flock can raise (EINTR on a signal, ENOLCK on some
        # filesystems); close the fd here or it leaks for the process lifetime,
        # since __exit__ never runs when __enter__ propagates.
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        with FileLock._guard:
            FileLock._fd_by_path[key] = fd
            FileLock._depth_by_path[key] = 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        key = str(self._lock_path)
        with FileLock._guard:
            depth = FileLock._depth_by_path.get(key, 0)
            if depth > 1:
                # An inner (reentrant) exit: keep the flock for the outer holder.
                FileLock._depth_by_path[key] = depth - 1
                return
            fd = FileLock._fd_by_path.pop(key, -1)
            FileLock._depth_by_path.pop(key, None)
        if fd < 0:
            return
        # flock releases on close; unlock first so a waiter wakes even if the
        # interpreter delays the close. Suppress an unlock error so the real
        # exception (if any) from the body propagates unmasked.
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
