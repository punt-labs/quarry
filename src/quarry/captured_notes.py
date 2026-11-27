"""Notes saying what keeps a covered child captured once its own marker is gone."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from quarry.parent_disable_command import ParentDisableCommand

if TYPE_CHECKING:
    from quarry.enable import DisableResult

__all__ = ["CapturedAt"]


@dataclass(frozen=True, slots=True)
class CapturedAt(ABC):
    """A note for one disable result that still has a capturing marker."""

    result: DisableResult

    @staticmethod
    def of(result: DisableResult) -> CapturedAt:
        """Pick the note for *result*'s marker layout.

        One marker at the registration's directory, one between the child and
        the registration, or several, where disabling one leaves the next found.
        """
        at_registration = result.capturing_marker == result.covered_by
        return _CASES[(bool(result.also_capturing), at_registration)](result)

    @abstractmethod
    def __str__(self) -> str:
        """Return the one-line note."""


@final
@dataclass(frozen=True, slots=True)
class AtRegistration(CapturedAt):
    """The only marker is at the registration's directory.

    Disabling there means deregistering and purging the registration.
    """

    def __str__(self) -> str:
        """Return the note naming the command that deregisters the parent."""
        r = self.result
        return (
            f"{r.directory} is still captured by the registration at "
            f"{r.covered_by}; `{ParentDisableCommand(r.covered_by)}` stops it "
            f"but deregisters {r.covered_by} and purges everything indexed "
            f"for {r.covered_by} (add --keep-data to keep the data)"
        )


@final
@dataclass(frozen=True, slots=True)
class AtIntermediate(CapturedAt):
    """The only marker is at a directory between the child and the registration.

    Disabling there touches only that directory's own files.
    """

    def __str__(self) -> str:
        """Return the note naming that directory's disable command."""
        r = self.result
        return (
            f"{r.directory} is still captured by the registration at "
            f"{r.covered_by} because of the enabled marker at "
            f"{r.capturing_marker}; `{ParentDisableCommand(r.capturing_marker)}` "
            f"stops it, as does disabling {r.covered_by}"
        )


@final
@dataclass(frozen=True, slots=True)
class AtSeveralMarkers(CapturedAt):
    """Name every marker: disabling one leaves the next still found."""

    def __str__(self) -> str:
        """Return the note listing each marker and each way to stop capture."""
        r = self.result
        markers = (r.capturing_marker, *r.also_capturing)
        commands = ", ".join(
            f"`{ParentDisableCommand(m)}`" for m in markers if m != r.covered_by
        )
        return (
            f"{r.directory} is still captured by the registration at "
            f"{r.covered_by}: enabled markers remain at {', '.join(markers)}; "
            f"capture continues until all of them are disabled"
            f"{f' ({commands})' if commands else ''}, or until "
            f"`{ParentDisableCommand(r.covered_by)}` deregisters {r.covered_by} "
            f"and purges everything indexed for {r.covered_by} "
            f"(add --keep-data to keep the data)"
        )


_CASES: dict[tuple[bool, bool], type[CapturedAt]] = {
    (True, True): AtSeveralMarkers,
    (True, False): AtSeveralMarkers,
    (False, True): AtRegistration,
    (False, False): AtIntermediate,
}
