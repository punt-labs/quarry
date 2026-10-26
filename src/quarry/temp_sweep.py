"""The temp files a crashed private write left behind: find them, remove the stale."""

from __future__ import annotations

import logging
import os
import re
import stat
import time
from collections import defaultdict
from enum import Enum, auto
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.failure_reason import FailureReason

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class Leftover(Enum):
    """What became of a leftover the sweep looked at."""

    REMOVED = auto()
    YOUNG = auto()  # too recent to be a crashed write's: a live writer may own it
    NON_REGULAR = auto()  # a symlink or directory: a sign of tampering
    FOREIGN = auto()  # owned by another user
    FAILED = auto()  # the filesystem refused; the entry carries the errno


@final
class TempSweep:
    """Remove the stale temps beside a private file, and say what it left and why.

    Only names the caller's pattern matches are looked at: for a private write,
    ``<name>.<12 hex>.tmp`` and the legacy ``<name>.tmp``.  Of those, a symlink or
    a directory is never touched (and is reported on its own, as it should not be
    there), neither is a file another user owns, and a regular file this user owns
    is removed once it is :attr:`STALE_AFTER_S` old or dated more than
    :attr:`CLOCK_SKEW_S` ahead.  One younger than that is kept and listed as
    young.  A leftover that vanishes mid-sweep (a live writer renamed it) counts as
    nothing.  The names are safe to log; an errno never quotes a path.
    """

    # Long past any live write: a temp this old belongs to a crashed one.
    STALE_AFTER_S: ClassVar[float] = 600.0
    # How far ahead of the clock an mtime may be before it is not a live writer's:
    # a clock that was stepped back, or a shared filesystem a little ahead of us.
    CLOCK_SKEW_S: ClassVar[float] = 3600.0

    __slots__ = ("_directory", "_label", "_name", "_stale_after_s")

    _directory: Path
    _label: str
    _name: re.Pattern[str]
    _stale_after_s: float

    def __new__(
        cls,
        directory: Path,
        name: re.Pattern[str],
        label: str,
        stale_after_s: float = STALE_AFTER_S,
    ) -> Self:
        self = super().__new__(cls)
        self._stale_after_s = stale_after_s
        self._label = label
        self._directory = directory
        self._name = name
        return self

    @classmethod
    def beside(cls, path: Path, label: str) -> Self:
        """Return the sweep of the temps ``PrivateWrite`` makes next to *path*."""
        name = re.compile(re.escape(path.name) + r"(?:\.[0-9a-f]{12})?\.tmp")
        return cls(path.parent, name, label)

    def run(self) -> None:
        """Sweep the directory once; never raise."""
        names = self._leftover_names()
        outcome: defaultdict[Leftover, list[str]] = defaultdict(list)
        for name in names:
            result = self._visit(name)
            if result is not None:
                outcome[result[0]].append(result[1])
        self._report(outcome)

    def _leftover_names(self) -> list[str]:
        """Return the sorted names that look like a crashed write's temp."""
        try:
            with os.scandir(self._directory) as entries:
                found = [e.name for e in entries if self._name.fullmatch(e.name)]
        except FileNotFoundError:
            return []  # nothing has been written here yet, so nothing was left
        except OSError as exc:
            logger.warning(
                "%s: could not list %s for leftover temp files (%s)",
                self._label,
                self._directory,
                FailureReason.os_name(exc),
            )
            return []
        return sorted(found)

    def _visit(self, name: str) -> tuple[Leftover, str] | None:
        """Return how *name* was dealt with, or ``None`` if it needs no word."""
        leftover = self._directory / name
        try:
            info = leftover.lstat()
            if not stat.S_ISREG(info.st_mode):
                return Leftover.NON_REGULAR, name
            if info.st_uid != os.getuid():
                return Leftover.FOREIGN, name
            if self._is_young(info.st_mtime):
                return Leftover.YOUNG, name
            leftover.unlink()
        except FileNotFoundError:
            return None  # lost a race with whoever renamed or removed it
        except OSError as exc:
            return Leftover.FAILED, f"{name} ({FailureReason.os_name(exc)})"
        return Leftover.REMOVED, name

    def _is_young(self, mtime: float) -> bool:
        """Return whether *mtime* is recent enough to belong to a live writer.

        With a stale age of zero nothing is young: the caller knows no writer is
        live, so every leftover is a crash's.
        """
        age = time.time() - mtime
        return (
            self._stale_after_s > 0 and -self.CLOCK_SKEW_S <= age < self._stale_after_s
        )

    def _report(self, outcome: dict[Leftover, list[str]]) -> None:
        """Warn once, with one labelled list of names per kind found."""
        titles = {
            Leftover.REMOVED: "removed",
            Leftover.FOREIGN: "foreign-owned, left",
            Leftover.NON_REGULAR: "non-regular (a symlink or directory), left",
            Leftover.FAILED: "failed",
            Leftover.YOUNG: "kept (young)",
        }
        found = [
            f"{title} ({len(outcome[kind])}): {', '.join(outcome[kind])}"
            for kind, title in titles.items()
            if outcome.get(kind)
        ]
        if not found:
            return
        # Young temps alone are a live writer's, so they are news, not a warning.
        level = logging.WARNING if set(outcome) - {Leftover.YOUNG} else logging.INFO
        logger.log(level, "%s: leftover temp files: %s", self._label, "; ".join(found))
