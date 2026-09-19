"""The ``missions_sync`` MCP tool — Loop 2 on the MCP surface."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.client import TargetResolver
from quarry.mcp_guard import ToolGuard
from quarry.mission_memory import MissionMemorySync
from quarry.mission_sync_types import SyncOptions

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.fastmcp import FastMCP

    from quarry.client import QuarryClient


@final
class MissionTools:
    """The mission-memory tool, a sibling of ``McpTools`` sharing its boundary.

    Holds only the client factory; the same :class:`MissionMemorySync` the CLI
    drives builds the identical request sequence from the same files.
    """

    __slots__ = ("_connect",)

    _connect: Callable[[], QuarryClient]

    def __new__(
        cls, connect: Callable[[], QuarryClient] = TargetResolver.connect
    ) -> Self:
        self = super().__new__(cls)
        self._connect = connect
        return self

    def register(self, server: FastMCP) -> None:
        """Attach the guarded tool to *server* under its wire name."""
        server.add_tool(self.missions_sync)

    @ToolGuard.wrap
    def missions_sync(
        self, *, mission: str = "", dry_run: bool = False, force: bool = False
    ) -> str:
        """Use after ``ethos mission close`` to file each frozen round into the
        worker's memory (``memory-<worker>``, type observation).

        Reads this repo's ``.punt-labs/ethos/missions/`` tree; safe to re-run —
        a round already filed is skipped, and a name held by another checkout
        is reported as an error, never overwritten.

        Args:
            mission: Sync one mission id only; empty means every mission.
            dry_run: List what would be filed and post nothing.
            force: Re-file rounds the daemon already holds (matching key only).
        """
        outcome = MissionMemorySync.for_repo(
            Path.cwd(),
            self._connect(),
            SyncOptions(mission_id=mission, dry_run=dry_run, force=force),
        )
        return outcome.render()
