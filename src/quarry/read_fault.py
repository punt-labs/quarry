"""A transcript that could not be read, told by what it means for the run."""

from __future__ import annotations

from typing import Self, final

from quarry.failure_reason import FailureReason
from quarry.refusal import RefusalError


@final
class ReadFault:
    """A transcript that could not be read, told by what it means for the run.

    A file that vanished after it was listed is not a fault of the run.  Any
    other (a full file table, a permission, a bad encoding, a transcript over the
    read limit) is, and :attr:`reason` names it without quoting a path.
    """

    __slots__ = ("_exc",)

    _exc: Exception

    def __new__(cls, exc: Exception) -> Self:
        self = super().__new__(cls)
        self._exc = exc
        return self

    @property
    def vanished(self) -> bool:
        """Return whether the file was gone by the time it was read."""
        return isinstance(self._exc, FileNotFoundError)

    @property
    def reason(self) -> str:
        """Return a refusal's own words, else the fault named by its errno."""
        if isinstance(self._exc, RefusalError):
            return str(self._exc)
        return f"unreadable ({FailureReason.os_name(self._exc)})"
