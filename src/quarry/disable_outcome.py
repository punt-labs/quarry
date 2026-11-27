"""The closed set of outcomes ``quarry disable`` reports, each owning its text."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, final

from quarry.enable_report import EnableReport

if TYPE_CHECKING:
    from quarry.enable import DisableResult

__all__ = [
    "AlreadyDisabled",
    "CoveredCaptured",
    "CoveredUncaptured",
    "Deregistered",
    "DisableOutcome",
]


class DisableOutcome(ABC):
    """One way a disable can end: a heading, a note, and the shared removals.

    The removal lines (config, import, marker, permissions) are the same in
    every outcome; the heading and note say what happened to the registration.
    """

    __slots__ = ()

    result: DisableResult
    _NOTE_LAST: ClassVar[bool] = False

    @abstractmethod
    def heading(self) -> str:
        """Return the first line of the report."""

    @abstractmethod
    def note(self) -> str:
        """Return the line describing the registration's fate."""

    @staticmethod
    def of(result: DisableResult, *, keep_data: bool) -> DisableOutcome:
        """Pick the outcome a disable ended in."""
        if result.covered_by:
            if result.parent_enabled:
                return CoveredCaptured(result)
            return CoveredUncaptured(result)
        if result.collection:
            return Deregistered(result, keep_data)
        return AlreadyDisabled(result)

    def lines(self) -> list[str]:
        """Return heading, note, and removal lines; the note last when asked."""
        removed = self._removed()
        if self._NOTE_LAST:
            return [self.heading(), *removed, self.note()]
        return [self.heading(), self.note(), *removed]

    def _removed(self) -> list[str]:
        r = self.result
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
@dataclass(frozen=True, slots=True)
class Deregistered(DisableOutcome):
    """The directory owned a registration, now deregistered."""

    result: DisableResult
    keep_data: bool

    def heading(self) -> str:
        """Return the disable heading."""
        return f"Disabled quarry for {self.result.directory}"

    def note(self) -> str:
        """Return the deregistration and what became of its indexed data."""
        r = self.result
        # Both keep-data branches report the registration so --keep-data does
        # not look like only local files were touched.
        fate = "kept indexed data" if self.keep_data else "chunk purge queued"
        return f"  Deregistered {r.collection} ({r.removed} files); {fate}"


@final
@dataclass(frozen=True, slots=True)
class AlreadyDisabled(DisableOutcome):
    """Nothing was registered: never enabled, or a prior disable finished."""

    result: DisableResult

    def heading(self) -> str:
        """Return the disable heading."""
        return f"Disabled quarry for {self.result.directory}"

    def note(self) -> str:
        """Return the idempotent no-op note."""
        return "  Already disabled (no registration)"


class _CoveredChild(DisableOutcome):
    """A directory under a parent registration, which a disable never touches."""

    __slots__ = ()

    _NOTE_LAST: ClassVar[bool] = True

    def heading(self) -> str:
        """Return what was removed from the child, or that nothing was."""
        where = self.result.directory
        if self._removed():
            return f"Removed quarry's own settings from {where}"
        return f"Nothing of its own found in {where}"


@final
@dataclass(frozen=True, slots=True)
class CoveredCaptured(_CoveredChild):
    """The parent's marker keeps capture live under the child."""

    result: DisableResult

    def note(self) -> str:
        """Return the command that stops the capture still running here."""
        r = self.result
        return (
            f"{r.directory} is still captured by the registration at "
            f"{r.covered_by}; run 'quarry disable {r.covered_by}' to stop it"
        )


@final
@dataclass(frozen=True, slots=True)
class CoveredUncaptured(_CoveredChild):
    """The parent is registered but has no marker, so nothing is captured."""

    result: DisableResult

    def note(self) -> str:
        """Return why no capture runs here, without suggesting a parent disable."""
        r = self.result
        return (
            f"{r.directory} is registered under {r.covered_by} but not captured: "
            f"no enabled marker at {r.covered_by}"
        )
