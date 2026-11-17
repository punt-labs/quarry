"""The scratch directory a repair builds the new table in."""

from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

_PREFIX = ".repair-"


@final
class RepairScratch:
    """A ``.repair-<pid>`` directory beside the database, removed when done.

    A run killed hard leaves its directory behind, so :meth:`open` first sweeps
    every ``.repair-*`` sibling.  The caller holds the run dir lock, so any such
    sibling belongs to a dead run.
    """

    __slots__ = ("_parent",)

    _parent: Path

    def __new__(cls, parent: Path) -> Self:
        self = super().__new__(cls)
        self._parent = parent
        return self

    @contextmanager
    def open(self) -> Generator[Path]:
        """Yield a fresh scratch path; remove it on exit, however it ends."""
        for stale in self._parent.glob(f"{_PREFIX}*"):
            shutil.rmtree(stale, ignore_errors=True)
        path = self._parent / f"{_PREFIX}{os.getpid()}"
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)
