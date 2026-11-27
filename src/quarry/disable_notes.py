"""The disable-report notes for a covered child: what captures it, what stayed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.config_verdict import ConfigVerdict
from quarry.parent_disable_command import ParentDisableCommand

if TYPE_CHECKING:
    from quarry.enable import DisableResult

__all__ = ["CapturedNote", "KeptConfigNote"]

_KEPT = "  Config file kept (it has settings of yours; delete it to drop them)"
_UNREADABLE = (
    "  Config file kept (it could not be read, so it is not applied; "
    "delete it if it is stale)"
)
_LINKED = "  config.md is a symlink and was left in place"
_WHY = {
    ConfigVerdict.UNREADABLE.value: _UNREADABLE,
    ConfigVerdict.LINKED.value: _LINKED,
}


@final
class CapturedNote:
    """Say which marker keeps a covered child captured and what stops it.

    The marker is at the registration's directory, where disabling means
    deregistering and purging the registration, or at a directory between the
    child and the registration, where disabling touches only that directory's
    own files.
    """

    __slots__ = ("_r",)

    _r: DisableResult

    def __new__(cls, result: DisableResult) -> Self:
        """Bind the note to one disable result with a capturing marker."""
        self = super().__new__(cls)
        self._r = result
        return self

    def __str__(self) -> str:
        """Return the one-line note."""
        r = self._r
        if r.capturing_marker == r.covered_by:
            return (
                f"{r.directory} is still captured by the registration at "
                f"{r.covered_by}; `{ParentDisableCommand(r.covered_by)}` stops it "
                f"but deregisters {r.covered_by} and purges everything indexed "
                f"for {r.covered_by} (add --keep-data to keep the data)"
            )
        return (
            f"{r.directory} is still captured by the registration at "
            f"{r.covered_by} because of the enabled marker at "
            f"{r.capturing_marker}; `{ParentDisableCommand(r.capturing_marker)}` "
            f"stops it, as does disabling {r.covered_by}"
        )


@final
class KeptConfigNote:
    """Say that a disable left ``config.md`` in place, and why."""

    __slots__ = ("_r",)

    _r: DisableResult

    def __new__(cls, result: DisableResult) -> Self:
        """Bind the note to one disable result."""
        self = super().__new__(cls)
        self._r = result
        return self

    def lines(self) -> list[str]:
        """Return the one line for a config left in place, else none."""
        r = self._r
        return [_WHY.get(r.config_why, _KEPT)] if r.config_kept else []
