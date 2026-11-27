"""Render a disable result into human-readable CLI summary lines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.disable_lines import CoveredDisableNote, RemovedItems

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
        r = self._r
        removed = RemovedItems(r).lines()
        if r.covered_by:
            return CoveredDisableNote(r.directory, r.covered_by, removed).lines()
        if r.collection:
            # Report the registration in both keep-data branches so --keep-data
            # does not look like only local files were touched.
            fate = "kept indexed data" if self._keep_data else "chunk purge queued"
            note = f"  Deregistered {r.collection} ({r.removed} files); {fate}"
        else:
            # Idempotent no-op: nothing was registered (never enabled, or a prior
            # partial disable already deregistered it).
            note = "  Already disabled (no registration)"
        return [f"Disabled quarry for {r.directory}", note, *removed]
