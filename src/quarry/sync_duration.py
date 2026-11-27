"""The clause saying what ends session start across several markers."""

from __future__ import annotations

from typing import Self, final

from quarry.marker_chain import MarkerChain
from quarry.parent_disable_command import ParentDisableCommand

__all__ = ["SyncDuration"]


@final
class SyncDuration:
    """Say how long session start stays active as the nearest markers are disabled.

    The clause lists disable commands only for markers whose removal ends sync.
    Removing a marker that opts out, or one beyond it, hands the decision to a
    farther marker that may allow sync, so those commands are left out. The
    registration's own command purges data and is never listed here.
    """

    __slots__ = ("_chain", "_registration")

    _chain: MarkerChain
    _registration: str

    def __new__(cls, markers: tuple[str, ...], registration: str) -> Self:
        """Bind the clause to *markers* and the covering *registration*."""
        self = super().__new__(cls)
        self._chain = MarkerChain(markers)
        self._registration = registration
        return self

    def opt_out(self) -> str:
        """Return the first marker whose config opts out of session sync, or ``""``."""
        return self._chain.opt_out()

    def _listed(self, stop: str, label: str = "") -> str:
        """Return the parenthesised disable commands for markers nearer than *stop*."""
        commands = ", ".join(
            f"`{ParentDisableCommand(m)}`"
            for m in self._chain.until(stop)
            if m != self._registration
        )
        return f" ({label}{commands})" if commands else ""

    def __str__(self) -> str:
        """Return the clause saying what ends session start."""
        if not (stop := self._chain.opt_out(1)):
            return f"it stays active until all of them are disabled{self._listed(stop)}"
        return (
            f"session_sync is false in the config at {stop}, so once the markers "
            f"nearer than it are disabled session start stops there, whatever a "
            f"farther marker allows{self._listed(stop, 'disabling each: ')}"
        )
