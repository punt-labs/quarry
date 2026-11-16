"""The ``quarry repair`` command: rebuild a chunks table optimize() cannot compact.

Unlike every other data command this one never talks to the daemon: it refuses
to run while one is up and works on the database directory directly.
"""

from __future__ import annotations

import importlib
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated, Protocol, Self, cast, final

import typer

from quarry.config import Settings
from quarry.daemon_guard import DaemonGuard
from quarry.db_pointer import SELECTION
from quarry.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from quarry.cli_captures import CliPlumbing
    from quarry.repair_plan import RepairPlan

_MIB = 2**20


class _Repair(Protocol):
    """The slice of ``TableRepair`` this command drives, loaded only on demand."""

    def plan(self) -> RepairPlan: ...
    def rebuild(self) -> int: ...


@final
class RepairCli:
    """Serve ``repair`` around an injected plumbing bundle."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach ``repair`` to *app*."""
        app.command(name="repair")(self._p.cli_errors(self._repair))

    def _repair(
        self,
        *,
        yes: Annotated[
            bool, typer.Option("--yes", "-y", help="Consent without prompting")
        ] = False,
        dry_run: Annotated[
            bool, typer.Option("--dry-run", help="Report what would be dropped only")
        ] = False,
    ) -> None:
        """Rebuild the chunks table without binary and over-budget rows.

        Stop the daemon first: the command refuses to run while quarryd is up.
        It rebuilds into a scratch directory, verifies every kept row and swaps
        the new table in atomically, so a failure leaves the original intact.
        Without ``--yes`` it names what it will drop and asks before changing
        anything.
        """
        db_path = Settings.load().resolve_db_paths(SELECTION.active()).lancedb_path
        if DaemonGuard(RunDir(db_path.parent)).is_running():
            self._p.err_console.print(
                "Error: quarryd is running; stop it first (the repair swaps the "
                "table directory out from under a running daemon)",
                style="red",
            )
            raise typer.Exit(code=1)
        # The engine (lancedb) loads only when a repair actually runs.
        engine = importlib.import_module("quarry.repair")
        repair = cast("Callable[[Path], _Repair]", engine.TableRepair)(db_path)
        plan = repair.plan()
        self._p.emit(asdict(plan), self._describe(plan))
        if dry_run or plan.dropped_rows == 0:
            return
        if not (yes or self._consents()):
            self._p.emit({"repaired": False}, "Cancelled; nothing was changed.")
            return
        size = repair.rebuild()
        self._p.emit(
            {"repaired": True, "on_disk_bytes": size},
            f"Repaired: kept {plan.kept_rows} rows, table is now {size / _MIB:.1f} MiB",
        )

    @staticmethod
    def _consents() -> bool:
        try:
            return typer.confirm("Delete these rows and rebuild the table?")
        except typer.Abort:
            return False

    @staticmethod
    def _describe(plan: RepairPlan) -> str:
        return (
            f"{plan.rows} rows: {plan.binary_rows} binary and "
            f"{plan.over_budget_rows} over-budget rows will be dropped, "
            f"{plan.kept_rows} kept.\n"
            f"live data {plan.live_bytes / _MIB:.1f} MiB, "
            f"on disk {plan.on_disk_bytes / _MIB:.1f} MiB"
        )
