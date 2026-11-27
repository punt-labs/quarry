"""The closed set of outcomes ``quarry disable`` reports, each owning its text."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from quarry.disable_lines import DisableLines

if TYPE_CHECKING:
    from quarry.enable import DisableResult

__all__ = [
    "AlreadyDisabled",
    "Covered",
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

    def heading(self) -> str:
        """Return the first line of the report."""
        return f"Disabled quarry for {self.result.directory}"

    @abstractmethod
    def note(self) -> str:
        """Return the line describing the registration's fate."""

    @staticmethod
    def of(result: DisableResult, *, keep_data: bool) -> DisableOutcome:
        """Pick the outcome a disable ended in."""
        if result.covered_by:
            return Covered(result)
        if result.collection:
            return Deregistered(result, keep_data)
        return AlreadyDisabled(result)

    def lines(self) -> list[str]:
        """Return the heading, then the note and removal lines in outcome order."""
        return [self.heading(), *self._body()]

    def _body(self) -> list[str]:
        return [self.note(), *self._removed()]

    def _changes(self) -> DisableLines:
        return DisableLines(self.result)

    def _removed(self) -> list[str]:
        return self._changes().removed() + self._changes().problems()


@final
@dataclass(frozen=True, slots=True)
class Deregistered(DisableOutcome):
    """The directory owned a registration, now deregistered."""

    result: DisableResult
    keep_data: bool

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

    def note(self) -> str:
        """Return the idempotent no-op note."""
        return "  Already disabled (no registration)"


class _CoveredChild(DisableOutcome):
    """A directory under a parent registration, which a disable never touches."""

    __slots__ = ()

    def _body(self) -> list[str]:
        # A config left in place is stated as plainly as a removal.
        return [*self._removed(), *self._changes().kept(), self.note()]

    def heading(self) -> str:
        """Return what was removed from the child, or that nothing was."""
        # Real removals only: a permissions error renders a line but removes nothing.
        if self._changes().removed():
            return f"Removed quarry's own settings from {self.result.directory}"
        return self._nothing_removed()

    def _nothing_removed(self) -> str:
        where = self.result.directory
        if self.result.config_kept:
            return f"Nothing removed from {where}"
        return f"Nothing of its own found in {where}"


@final
@dataclass(frozen=True, slots=True)
class Covered(_CoveredChild):
    """A child under a parent registration: what keeps session start active, if any."""

    result: DisableResult

    def note(self) -> str:
        """Return the marker that still activates session start, or that none does."""
        return self._changes().captured()
