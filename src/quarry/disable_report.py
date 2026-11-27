"""Render a disable result into human-readable CLI summary lines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.disable_outcome import DisableOutcome

if TYPE_CHECKING:
    from quarry.enable import DisableResult

__all__ = ["DisableReport"]


@final
class DisableReport:
    """Format a :class:`DisableResult` as the lines ``quarry disable`` prints."""

    __slots__ = ("_keep_data", "_r")

    _r: DisableResult
    _keep_data: bool

    def __new__(cls, result: DisableResult, *, keep_data: bool) -> Self:
        """Pair a disable outcome with the caller's keep-data reporting policy."""
        self = super().__new__(cls)
        self._r = result
        self._keep_data = keep_data
        return self

    def lines(self) -> list[str]:
        """Return the summary lines for the disable, in display order."""
        return DisableOutcome.of(self._r, keep_data=self._keep_data).lines()
