"""The enabled markers a covered child still has, nearest first."""

from __future__ import annotations

from typing import Self, final

from quarry.governing_marker import GoverningMarker

__all__ = ["MarkerChain"]


@final
class MarkerChain:
    """Order the markers whose config the session-start hook reads, nearest first.

    Disabling the nearest marker hands the decision to the next one, and the
    hook stops at it: that marker's ``session_sync: false`` blocks sync even when
    a farther marker allows it.
    """

    __slots__ = ("_markers",)

    _markers: tuple[str, ...]  # nearest first

    def __new__(cls, markers: tuple[str, ...]) -> Self:
        """Bind the chain to its *markers*."""
        self = super().__new__(cls)
        self._markers = markers
        return self

    def opt_out(self, start: int = 0) -> str:
        """Return the first marker from *start* whose config opts out, or ``""``."""
        syncs = GoverningMarker.syncs_at
        return next((m for m in self._markers[start:] if not syncs(m)), "")

    def until(self, stop: str) -> tuple[str, ...]:
        """Return the markers nearer than *stop*, or all of them when it is empty."""
        return self._markers[: self._markers.index(stop)] if stop else self._markers
