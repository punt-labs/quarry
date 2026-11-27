"""Render a disable result into human-readable CLI summary lines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.enable_report import EnableReport

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
        lines = [f"Disabled quarry for {r.directory}"]
        if r.covered_by:
            lines.append(f"  Kept parent registration at {r.covered_by}")
        elif r.collection:
            # Report the registration in both keep-data branches so --keep-data
            # does not look like only local files were touched.
            fate = "kept indexed data" if self._keep_data else "chunk purge queued"
            lines.append(f"  Deregistered {r.collection} ({r.removed} files); {fate}")
        else:
            # Idempotent no-op: nothing was registered (never enabled, or a prior
            # partial disable already deregistered it).
            lines.append("  Already disabled (no registration)")
        lines += EnableReport.flagged(
            (
                (r.config_removed, "  Config file removed"),
                (
                    r.import_pruned,
                    "  Removed @.punt-labs/quarry/CLAUDE.md from CLAUDE.md",
                ),
                (
                    r.enabled_marker_removed,
                    "  Removed enabled marker (guide left dormant)",
                ),
            )
        )
        return lines + r.permissions.lines("Removed quarry tool permissions from")
