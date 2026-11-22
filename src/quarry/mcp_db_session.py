"""Choose the local database for the lifetime of one MCP stdio process."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.client import TargetResolver
from quarry.config import Settings
from quarry.db_pointer import SELECTION

if TYPE_CHECKING:
    from quarry.db_pointer import DatabaseSelection


@final
class McpDatabaseSession:
    """Apply startup and tool-requested choices without persisting them."""

    __slots__ = ("_selection",)

    _selection: DatabaseSelection

    def __new__(cls, selection: DatabaseSelection = SELECTION) -> Self:
        """Bind the holder of the process-local database override."""
        self = super().__new__(cls)
        self._selection = selection
        return self

    def select_startup(self, db_name: str | None) -> None:
        """Use the launcher's choice or clear the process-local override."""
        self._selection.override(db_name or "")

    def switch_local(self, name: str) -> str:
        """Select a named local database and report the switch or refusal."""
        # A local db selection only governs the loopback run-dir target. Under a
        # remote/explicit target it is IGNORED by TargetResolver — so refuse
        # rather than report a switch that silently doesn't take effect and could
        # read or destroy data on the wrong (remote) daemon.
        if not TargetResolver.selects_local_db():
            return (
                "Error: no local switch — a remote quarry target is active "
                "(QUARRY_URL or 'quarry login'). The remote daemon is fixed to "
                "its own database; 'use' only selects among local databases. Run "
                "'quarry logout' or unset QUARRY_URL to return to the local daemon."
            )
        previous = self._selection.active() or "default"
        # Select the literal named db, "default" included — never fall through to
        # the persisted default, or use("default") would silently pick
        # whatever the CLI last persisted and the summary path would lie about the
        # target subsequent tools connect to. Validate before mutating:
        # resolve_db_paths raises ValueError on a bad name, leaving the db unchanged.
        resolved = Settings.load().resolve_db_paths(name)
        self._selection.override(name)
        return f"\u25b6  Switched {previous} \u2192 {name} ({resolved.lancedb_path})"
