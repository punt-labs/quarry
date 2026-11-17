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
from quarry.db_pointer import SELECTION
from quarry.repair_plan import MIB

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from pathlib import Path

    from quarry.cli_captures import CliPlumbing
    from quarry.repair_plan import RepairPlan


class _Session(Protocol):
    """The slice of ``TableRepair`` a locked session drives."""

    def plan(self) -> RepairPlan: ...
    def rebuild(self) -> int: ...


class _Repair(Protocol):
    """The slice of ``TableRepair`` this command drives, loaded only on demand."""

    def session(self) -> AbstractContextManager[_Session]: ...


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

        If a crash leaves the table in ``.chunks.lance.bak-<pid>`` beside it,
        repair refuses to run; restore it with
        ``mv .chunks.lance.bak-<pid> chunks.lance`` and run again.
        """
        settings = Settings.load().resolve_db_paths(SELECTION.active())
        db_path = settings.lancedb_path
        # The engine (lancedb) loads only when a repair actually runs.
        engine = importlib.import_module("quarry.repair")
        repair = cast("Callable[[Path, Path], _Repair]", engine.TableRepair)(
            db_path, settings.quarry_root
        )
        try:
            with repair.session() as session:
                self._run(session, yes=yes, dry_run=dry_run)
        except ValueError as exc:
            self._p.err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc

    def _run(self, session: _Session, *, yes: bool, dry_run: bool) -> None:
        """Plan, ask and rebuild without ever letting go of the daemon locks."""
        plan = session.plan()
        self._p.emit(asdict(plan), plan.describe())
        if dry_run or not plan.needs_repair:
            return
        if not (yes or self._consents(plan)):
            self._p.emit({"repaired": False}, "Cancelled; nothing was changed.")
            return
        size = session.rebuild()
        self._p.emit(
            {"repaired": True, "on_disk_bytes": size},
            f"Repaired: kept {plan.kept_rows} rows, table is now {size / MIB:.1f} MiB",
        )

    @staticmethod
    def _consents(plan: RepairPlan) -> bool:
        question = (
            "Delete these rows and rebuild the table?"
            if plan.dropped_rows
            else "Rebuild the table to reclaim the leaked disk space?"
        )
        try:
            return typer.confirm(question)
        except typer.Abort:
            return False
