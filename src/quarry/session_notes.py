"""Notes saying whether session start is active in a covered child, and why."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from quarry.marker_chain import MarkerChain
from quarry.parent_disable_command import ParentDisableCommand

if TYPE_CHECKING:
    from quarry.enable import DisableResult

__all__ = ["SessionNote"]


@dataclass(frozen=True, slots=True)
class SessionNote(ABC):
    """A note for one disable result on a child below a parent registration."""

    result: DisableResult

    @staticmethod
    def of(result: DisableResult) -> SessionNote:
        """Pick the note for *result*'s marker layout.

        No marker, one marker at the registration's directory, one between the
        child and the registration, or several, where disabling one leaves the
        next found.
        """
        if not result.capturing_marker:
            return NotActive(result)
        if opted_out := SessionNote._opted_out_at(result):
            return OptedOut(result, opted_out)
        at_registration = result.capturing_marker == result.covered_by
        return _CASES[(bool(result.also_capturing), at_registration)](result)

    @staticmethod
    def _opted_out_at(result: DisableResult) -> str:
        """Return the directory whose config turns session sync off, or ``""``.

        The hook reads the session directory's config first, then the governing
        marker's, through the same fail-open loader, so this does too.
        """
        return MarkerChain((result.directory, result.capturing_marker), "").opt_out()

    def _active(self, where: str) -> str:
        """Return the sentence saying what keeps the directory active through *where*.

        The marker gates session start only; the other hooks read auto_capture.
        """
        return (
            f"{self.result.directory} is still active through {where} "
            f"(session-start sync runs there); web, transcript and other "
            f"capture hooks follow their own auto_capture settings"
        )

    @abstractmethod
    def __str__(self) -> str:
        """Return the one-line note."""


@final
@dataclass(frozen=True, slots=True)
class NotActive(SessionNote):
    """No readable enabled marker lies between the child and the registration."""

    def __str__(self) -> str:
        """Return the note naming the directories searched."""
        r = self.result
        return (
            f"{r.directory} is registered under {r.covered_by} but session start "
            f"does not activate quarry there: no readable enabled marker from "
            f"{r.directory} up to {r.covered_by}"
        )


@final
@dataclass(frozen=True, slots=True)
class OptedOut(SessionNote):
    """A config on the session's path sets ``session_sync: false``."""

    directory: str

    def __str__(self) -> str:
        """Return the note naming the config that opts session start out."""
        r = self.result
        return (
            f"{r.directory} is registered under {r.covered_by} but session start "
            f"does not activate quarry there: session_sync is false in the config "
            f"at {self.directory}; web, transcript and other capture hooks follow "
            f"their own auto_capture settings"
        )


@final
@dataclass(frozen=True, slots=True)
class AtRegistration(SessionNote):
    """The only marker is at the registration's directory.

    Disabling there means deregistering and purging the registration.
    """

    def __str__(self) -> str:
        """Return the note naming the command that deregisters the parent."""
        r = self.result
        return (
            f"{self._active(f'the marker at {r.covered_by}')}; "
            f"`{ParentDisableCommand(r.covered_by)}` ends it but deregisters "
            f"{r.covered_by} and purges everything indexed for {r.covered_by} "
            f"(add --keep-data to keep the data)"
        )


@final
@dataclass(frozen=True, slots=True)
class AtIntermediate(SessionNote):
    """The only marker is at a directory between the child and the registration.

    Disabling there touches only that directory's own files.
    """

    def __str__(self) -> str:
        """Return the note naming that directory's disable command."""
        r = self.result
        return (
            f"{self._active(f'the marker at {r.capturing_marker}')}; "
            f"`{ParentDisableCommand(r.capturing_marker)}` ends it, "
            f"as does disabling {r.covered_by}"
        )


@final
@dataclass(frozen=True, slots=True)
class AtSeveralMarkers(SessionNote):
    """Name every marker: the hook reads the nearest one's config, then the next."""

    def __str__(self) -> str:
        """Return the note listing each marker and each way to end session start."""
        r = self.result
        markers = (r.capturing_marker, *r.also_capturing)
        commands = ", ".join(
            f"`{ParentDisableCommand(m)}`" for m in markers if m != r.covered_by
        )
        return (
            f"{self._active(f'the markers at {", ".join(markers)}')}; "
            f"{MarkerChain(markers, commands).duration()}; "
            f"`{ParentDisableCommand(r.covered_by)}` deregisters {r.covered_by} "
            f"and purges everything indexed for {r.covered_by} "
            f"(add --keep-data to keep the data)"
        )


_CASES: dict[tuple[bool, bool], type[SessionNote]] = {
    (True, True): AtSeveralMarkers,
    (True, False): AtSeveralMarkers,
    (False, True): AtRegistration,
    (False, False): AtIntermediate,
}
