"""The shell command that disables a covering parent in the caller's database."""

from __future__ import annotations

import shlex
from typing import Self, final

from quarry.db_pointer import SELECTION

__all__ = ["ParentDisableCommand"]


@final
class ParentDisableCommand:
    """``quarry [--db NAME] disable PARENT``, ready to paste into a shell.

    The command runs in a new process, which knows this one's database only if
    the command repeats it; without ``--db`` it would act on the default
    database. Every word goes through ``shlex`` so a path with spaces or a quote
    stays one argument.
    """

    __slots__ = ("_parent",)

    _parent: str

    def __new__(cls, parent: str) -> Self:
        """Bind the command to the covering registration's directory."""
        self = super().__new__(cls)
        self._parent = parent
        return self

    def __str__(self) -> str:
        """Return the quoted command line."""
        db = SELECTION.active()
        flags = ["--db", db] if db else []
        return shlex.join(["quarry", *flags, "disable", self._parent])
