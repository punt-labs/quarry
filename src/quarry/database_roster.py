"""Which database directories under the data root a daemon may open."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_REGISTRY = "registry.db"
_DAEMON_TRACES = ("serve.lock", "serve.port")


@final
class DatabaseRoster:
    """The one rule for which directories under *root* are databases a daemon opens.

    A daemon started for one database watches every other directory that holds a
    ``registry.db``, so those are the members the watch loop opens.  A guard that
    keeps a daemon out must also see a database that has booted but has no
    registry yet, so :meth:`with_daemon_traces` adds any directory that holds a
    daemon's ``serve.lock`` or ``serve.port``.
    """

    __slots__ = ("_root",)

    _root: Path

    def __new__(cls, root: Path) -> Self:
        """Anchor named-database enumeration at the supplied database root."""
        self = super().__new__(cls)
        self._root = root
        return self

    def members(self) -> list[str]:
        """Return the sorted names of the directories that hold a registry."""
        return self._named(lambda entry: (entry / _REGISTRY).exists())

    def with_daemon_traces(self) -> list[str]:
        """Return :meth:`members` plus every directory a daemon has made its own."""
        return self._named(
            lambda entry: (
                (entry / _REGISTRY).exists()
                or any((entry / trace).exists() for trace in _DAEMON_TRACES)
            )
        )

    def _named(self, wanted: Callable[[Path], bool]) -> list[str]:
        # A missing root means no database has been created yet.
        names: list[str] = []
        with suppress(FileNotFoundError):
            names = sorted(
                entry.name
                for entry in self._root.iterdir()
                if entry.is_dir() and wanted(entry)
            )
        return names
