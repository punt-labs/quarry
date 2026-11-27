"""Find the enabled marker that governs a session's working directory."""

from __future__ import annotations

from pathlib import Path
from typing import Self, final

from quarry.enabled_marker import EnabledMarker

__all__ = ["MarkerSearch"]


@final
class MarkerSearch:
    """Look for ``.punt-labs/quarry/enabled`` from a directory up to a root.

    A session opened in a subdirectory of an enabled repo has no marker of its
    own, and a child enabled under a parent registration keeps its marker at the
    child's root while the registration names the parent. The search therefore
    checks the directory, each ancestor up to the registration's directory, and
    that directory. With no registration only the directory itself is checked.
    """

    __slots__ = ("_trail",)

    _trail: tuple[Path, ...]  # the directories to check, nearest first

    def __new__(cls, directory: Path, registered: str) -> Self:
        """Bind a search to *directory* and its covering registration, if any."""
        self = super().__new__(cls)
        root = Path(registered or directory)  # no registration: its own root
        # The root is always checked, even if the paths disagree on symlinks and
        # it is not found above the directory.
        ancestry = (directory, *directory.parents)
        climbed = ancestry[: ancestry.index(root) + 1] if root in ancestry else ()
        self._trail = tuple(dict.fromkeys((directory, *climbed, root)))
        return self

    def found_at(self) -> Path | None:
        """Return the nearest directory on the search path holding the marker.

        None is the contract for "no marker anywhere on the path".
        """
        return next((d for d in self._trail if EnabledMarker(d).is_present()), None)

    def markers(self) -> tuple[Path, ...]:
        """Return every directory on the search path holding the marker, nearest first.

        A disable that must say what keeps a directory captured needs all of
        them: disabling the nearest leaves the next one still found.
        """
        return tuple(d for d in self._trail if EnabledMarker(d).is_present())
