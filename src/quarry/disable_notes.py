"""The disable-report notes for a covered child: what captures it, what stayed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.config_verdict import ConfigVerdict
from quarry.session_notes import SessionNote

if TYPE_CHECKING:
    from quarry.enable import DisableResult

__all__ = ["CapturedNote", "KeptConfigNote"]

_KEPT = "  Config file kept (it has settings of yours; delete it to drop them)"
_UNREADABLE = (
    "  Config file kept (it could not be read, so it is not applied; "
    "delete it if it is stale)"
)
_LINKED = (
    "  config.md is behind a symlink (the file or a parent directory) "
    "and was left in place"
)
_WHY = {
    ConfigVerdict.UNREADABLE.value: _UNREADABLE,
    ConfigVerdict.LINKED.value: _LINKED,
}


@final
class CapturedNote:
    """Say which markers keep a covered child captured and what stops it.

    The note is chosen by the marker layout (:meth:`SessionNote.of`).
    """

    __slots__ = ("_r",)

    _r: DisableResult

    def __new__(cls, result: DisableResult) -> Self:
        """Bind the note to one disable result with a capturing marker."""
        self = super().__new__(cls)
        self._r = result
        return self

    def __str__(self) -> str:
        """Return the one-line note for this result's marker layout."""
        return str(SessionNote.of(self._r))


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
