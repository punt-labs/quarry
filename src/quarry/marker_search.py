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

    __slots__ = ("_directory", "_root")

    _directory: Path
    _root: Path | None  # None: the directory is not under any registration

    def __new__(cls, directory: Path, registered: str) -> Self:
        """Bind a search to *directory* and its covering registration, if any."""
        self = super().__new__(cls)
        self._directory = directory
        self._root = Path(registered) if registered else None
        return self

    def found_at(self) -> Path | None:
        """Return the nearest directory on the search path holding the marker.

        None is the contract for "no marker anywhere on the path".
        """
        return next(filter(lambda d: EnabledMarker(d).is_present(), self._path()), None)

    def _path(self) -> tuple[Path, ...]:
        """Return the directories to check, nearest first."""
        root = self._root
        if root is None:
            return (self._directory,)
        ancestry = (self._directory, *self._directory.parents)
        # The root is always checked, even if the paths disagree on symlinks and
        # it is not found above the directory.
        climbed = ancestry[: ancestry.index(root) + 1] if root in ancestry else ()
        return (self._directory, *climbed, root)
