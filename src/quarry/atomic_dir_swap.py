"""Atomic directory swap: populate a temp sibling, then rename it into place.

Extracted out of :mod:`quarry.skills_source`, whose ``deposit`` used to own
this mechanic inline alongside hashing and manifest I/O. Nothing here knows
about skills, manifests, or content hashes — only that one caller-populated
temp directory must land atomically at one target path, with the prior
occupant preserved until the swap is proven to succeed.
"""

from __future__ import annotations

import glob
import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["AtomicDirectorySwap", "AtomicSwapError"]


@final
@dataclass(eq=False)
class AtomicSwapError(OSError):
    """A swap failed and the previous occupant could not be restored either.

    Carries *backup* — the sibling directory still holding the pre-swap
    content — so an operator can recover by hand. Losing this path in a bare
    re-raise would strand the target in a state nothing can name: the
    original exception says the swap failed, but not where the last-known-good
    copy ended up. ``@dataclass(eq=False)`` rather than ``frozen`` for the
    same reason as ``quarry.client.errors``: an exception must let the
    interpreter set ``__traceback__``/``__cause__`` as it propagates, which a
    frozen ``__setattr__`` would block.
    """

    _message: str
    _backup: Path

    @property
    def backup(self) -> Path:
        return self._backup

    def __str__(self) -> str:
        return self._message


@final
class AtomicDirectorySwap:
    """Stage a temp sibling of *target*, then swap it in atomically.

    Two-phase: :meth:`prepare` returns an empty temp path for the caller to
    populate (e.g. ``shutil.copytree`` plus writing a manifest into it);
    :meth:`commit` renames it into place, backing up and restoring any prior
    occupant on failure. Splitting the phases lets the caller's own
    population step raise and clean up the temp directory on its own terms
    (:meth:`quarry.skills_source.SkillSource.deposit` does), without this
    class needing to know what "populate" means.
    """

    __slots__ = ("_name", "_target")

    _name: str
    _target: Path

    def __new__(cls, name: str, target: Path) -> Self:
        self = super().__new__(cls)
        self._name = name
        self._target = target
        return self

    def prepare(self) -> Path:
        """Ensure *target*'s parent exists, sweep stale siblings, return a tmp path.

        A stray temp/backup sibling from a prior run that crashed mid-swap (a
        different pid than this one, so the pid-scoped name below never
        collides with it) is swept first — otherwise it lingers forever.
        """
        self._target.parent.mkdir(parents=True, exist_ok=True)
        self._sweep_orphans()
        return self._target.parent / f".{self._target.name}.tmp-{os.getpid()}"

    def commit(self, tmp: Path) -> None:
        """Rename *tmp* into *target*, backing up and restoring on failure.

        The backup-rename and the tmp-rename share one ``try`` so a failure
        at EITHER step cleans up the freshly-populated *tmp* — a failed
        backup-rename used to happen before any ``try``, leaking *tmp* until
        the next swap's orphan sweep happened to find it.

        Raises :class:`AtomicSwapError` — chaining the original failure —
        only in the double-failure case: the swap itself failed AND
        restoring the backup also failed. Every other failure (a swap
        failure with no prior occupant to restore) re-raises the original
        ``OSError`` unchanged.
        """
        backup = self._target.parent / f".{self._target.name}.bak-{os.getpid()}"
        had_target = self._target.exists()
        try:
            if had_target:
                self._target.rename(backup)
            tmp.rename(self._target)
        except OSError as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            # ``backup.exists()`` distinguishes the two failure sites: if the
            # backup rename itself failed, *target* was never touched (POSIX
            # rename is all-or-nothing) and there is nothing to restore.
            if not had_target or not backup.exists():
                raise
            self._restore_backup(backup, exc)
        else:
            if had_target:
                shutil.rmtree(backup, ignore_errors=True)

    def _restore_backup(self, backup: Path, cause: OSError) -> None:
        """Restore *backup* to the target after a failed swap, then re-raise.

        Raises the chained :class:`AtomicSwapError` only if the restore
        itself fails — the double-failure case where *backup* is the only
        remaining copy of the pre-swap content.
        """
        try:
            backup.rename(self._target)
        except OSError as restore_exc:
            msg = (
                f"swap of {self._name!r} failed ({cause}) and restoring the "
                f"previous copy from {backup} also failed -- the previous "
                "copy is preserved there for manual recovery"
            )
            raise AtomicSwapError(msg, backup) from restore_exc
        raise cause

    def _sweep_orphans(self) -> None:
        """Delete stale ``.<name>.tmp-*``/``.<name>.bak-*`` siblings of *target*.

        Pid-scoped naming (see :meth:`prepare`/:meth:`commit`) means a run's
        own temp and backup directories never collide with a concurrent
        run's — but it also means a run that crashed mid-swap leaves its
        pid-named orphan behind forever, since nothing with that pid will
        ever clean it up. Swept unconditionally at the start of every
        :meth:`prepare`.

        *name* is escaped with :func:`glob.escape` before it goes into the
        pattern: a target literally named e.g. ``*`` would otherwise turn
        ``.{name}.tmp-*`` into the wildcard ``.*.tmp-*``, matching -- and
        deleting -- unrelated siblings' orphaned temp/backup directories.
        """
        escaped = glob.escape(self._target.name)
        parent = self._target.parent
        for pattern in (f".{escaped}.tmp-*", f".{escaped}.bak-*"):
            for orphan in parent.glob(pattern):
                if orphan.is_dir():
                    shutil.rmtree(orphan, ignore_errors=True)
                else:
                    orphan.unlink(missing_ok=True)
