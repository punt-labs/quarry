"""The text fragments of a disable report: what changed, what stayed, what to run."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.disable_notes import CapturedNote, KeptConfigNote
from quarry.enable_report import EnableReport

if TYPE_CHECKING:
    from quarry.enable import DisableResult

__all__ = ["DisableLines"]


@final
class DisableLines:
    """Config, import, marker and permission lines, and the parent-disable command."""

    __slots__ = ("_r",)

    _r: DisableResult

    def __new__(cls, result: DisableResult) -> Self:
        """Bind the lines to one disable result."""
        self = super().__new__(cls)
        self._r = result
        return self

    def removed(self) -> list[str]:
        """Return a line per real removal; a permissions error removes nothing."""
        r = self._r
        return EnableReport.flagged(
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
                (
                    r.permissions.changed,
                    "".join(
                        r.permissions.lines("Removed quarry tool permissions from")
                    ),
                ),
            )
        )

    def problems(self) -> list[str]:
        """Return the permissions-error line, if one was raised."""
        p = self._r.permissions
        return p.lines("") if p.error else []

    def kept(self) -> list[str]:
        """Return the line saying the config stayed, and why, if it did."""
        return KeptConfigNote(self._r).lines()

    def captured(self) -> str:
        """Return the note naming the marker that still captures the directory."""
        return str(CapturedNote(self._r))
