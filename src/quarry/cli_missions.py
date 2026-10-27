"""The ``quarry missions`` command group: file mission rounds as memories."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.mission_memory import MissionMemorySync
from quarry.mission_sync_types import SyncOptions

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing


@final
class MissionsCli:
    """Build the ``missions`` Typer sub-app around an injected plumbing bundle."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def build(self) -> typer.Typer:
        """Return the ``missions`` sub-app with its ``sync`` command."""
        app = typer.Typer(
            help="File each frozen ethos mission round into the worker's memory.",
            invoke_without_command=True,
            rich_markup_mode=None,
        )
        app.callback(invoke_without_command=True)(self._callback)
        app.command(name="sync")(self._p.cli_errors(self._sync))
        return app

    def _callback(self, ctx: typer.Context) -> None:
        """File ethos mission rounds as memories."""
        if ctx.invoked_subcommand is None:
            self._p.err_console.print(
                "Error: specify a subcommand — sync.", style="red"
            )
            raise typer.Exit(code=1)

    def _sync(
        self,
        *,
        mission: Annotated[
            str, typer.Option("--mission", help="Sync one mission id only")
        ] = "",
        dry_run: Annotated[
            bool,
            typer.Option("--dry-run", help="List what would be filed; post nothing"),
        ] = False,
        force: Annotated[
            bool,
            typer.Option("--force", help="Re-file rounds already held (same key only)"),
        ] = False,
    ) -> None:
        """File every frozen round of this repo's missions into memory-<worker>.

        Run after ``ethos mission close``; safe to re-run — a round already
        filed is skipped, and a name held by another checkout is an error,
        never an overwrite.
        """
        outcome = MissionMemorySync.for_repo(
            Path.cwd(),
            self._p.client(),
            SyncOptions(mission_id=mission, dry_run=dry_run, force=force),
        )
        self._p.emit(outcome.to_dict(), outcome.render())
        if outcome.has_errors:
            raise typer.Exit(code=1)
