"""The enabled marker that governs a session-start, and the config it speaks for."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry._stdlib import load_hook_config
from quarry.marker_search import MarkerSearch
from quarry.session_start_reply import SessionStartReply

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.sync_registry import DirectoryRegistration

__all__ = ["GoverningMarker"]


@final
class GoverningMarker:
    """Resolve the nearest enabled marker for a session and read its settings.

    A session below a directory that enabled quarry has no config of its own, so
    that directory's ``auto_capture`` settings govern it.  At the session's own
    directory the caller has already read its config.
    """

    __slots__ = ("_directory", "_found")

    _directory: Path
    _found: Path | None  # the nearest directory holding the marker; None if none

    def __new__(
        cls, directory: Path, registration: DirectoryRegistration | None
    ) -> Self:
        """Search from *directory* up to the directory of its covering registration."""
        self = super().__new__(cls)
        self._directory = directory
        covering = registration.directory if registration else ""
        self._found = MarkerSearch(directory, covering).found_at()
        return self

    def present(self) -> bool:
        """Return whether an enabled marker governs the session."""
        return self._found is not None

    @staticmethod
    def syncs_at(directory: str) -> bool:
        """Return whether the config in *directory* lets session sync run."""
        return load_hook_config(directory).session_sync

    def run(
        self,
        active: Callable[[str | None], SessionStartReply],
        collection: str | None,
    ) -> SessionStartReply:
        """Return ``active(collection)``, or a skip when the config opts out of sync.

        *active* is not called on a skip: it launches the background sync.
        """
        if self._found in (None, self._directory):
            return active(collection)
        if self.syncs_at(str(self._found)):
            return active(collection)
        return SessionStartReply({}, "skip", "config")
