"""The lines of a disable report that describe local removals and a parent's hold."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.enable_report import EnableReport

if TYPE_CHECKING:
    from quarry.enable import DisableResult

__all__ = ["CoveredDisableNote", "RemovedItems"]


@final
class RemovedItems:
    """One line per local file or setting a disable removed."""

    __slots__ = ("_r",)

    _r: DisableResult

    def __new__(cls, result: DisableResult) -> Self:
        """Bind the disable outcome whose removals are listed."""
        self = super().__new__(cls)
        self._r = result
        return self

    def lines(self) -> list[str]:
        """Return the removal lines, empty when nothing local was removed."""
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
            )
        ) + r.permissions.lines("Removed quarry tool permissions from")


@final
class CoveredDisableNote:
    """The lines ``quarry disable`` prints for a child of a registered parent.

    The parent's marker keeps capture live under the child, so the note names
    the command that stops it instead of claiming a disable.
    """

    __slots__ = ("_covered_by", "_directory", "_removed")

    _directory: str
    _covered_by: str
    _removed: list[str]

    def __new__(cls, directory: str, covered_by: str, removed: list[str]) -> Self:
        """Pair the child, its covering parent, and the lines for what was removed."""
        self = super().__new__(cls)
        self._directory = directory
        self._covered_by = covered_by
        self._removed = removed
        return self

    def lines(self) -> list[str]:
        """Return what was removed (or that nothing was), then the parent's hold."""
        head = (
            f"Removed quarry files from {self._directory}"
            if self._removed
            else f"Nothing of its own found in {self._directory}"
        )
        still = (
            f"{self._directory} is still captured by the registration at "
            f"{self._covered_by}; run 'quarry disable {self._covered_by}' to stop it"
        )
        return [head, *self._removed, still]
