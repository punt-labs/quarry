"""The enabled markers a covered child still has, nearest first."""

from __future__ import annotations

from typing import Self, final

from quarry.governing_marker import GoverningMarker

__all__ = ["MarkerChain"]


@final
class MarkerChain:
    """Say how long session start stays active as the nearest markers are disabled.

    Disabling the nearest marker hands the decision to the next one, and the
    hook stops at it: that marker's ``session_sync: false`` blocks sync even when
    a farther marker allows it.
    """

    __slots__ = ("_commands", "_markers")

    _markers: tuple[str, ...]  # nearest first
    _commands: str  # the disable commands, already joined for display

    def __new__(cls, markers: tuple[str, ...], commands: str) -> Self:
        """Bind the chain to its *markers* and their *commands* text."""
        self = super().__new__(cls)
        self._markers = markers
        self._commands = commands
        return self

    def opt_out(self, start: int = 0) -> str:
        """Return the first marker from *start* whose config opts out, or ``""``."""
        syncs = GoverningMarker.syncs_at
        return next((m for m in self._markers[start:] if not syncs(m)), "")

    def duration(self) -> str:
        """Return the clause saying what ends session start."""
        if not (stop := self.opt_out(1)):
            listed = f" ({self._commands})" if self._commands else ""
            return f"it stays active until all of them are disabled{listed}"
        each = f" (disabling each: {self._commands})" if self._commands else ""
        return (
            f"session_sync is false in the config at {stop}, so once the markers "
            f"nearer than it are disabled session start stops there, whatever a "
            f"farther marker allows{each}"
        )
