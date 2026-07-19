"""The ``quarry optimize`` and ``quarry backfill-sessions`` maintenance commands.

Both dispatch a singleton 202 task on the daemon and return immediately with the
task id (fire-and-forget, DES-001): the daemon validates synchronously before the
202, so a rejection (e.g. a 409 concurrent run) still raises and exits non-zero
via the shared error decorator; only the processing is not awaited.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.api import BackfillRequest, OptimizeRequest

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing


@final
class MaintenanceCli:
    """Serve ``optimize``/``backfill-sessions`` around an injected plumbing bundle."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach ``optimize`` and ``backfill-sessions`` to *app*."""
        app.command(name="optimize")(self._p.cli_errors(self._optimize))
        app.command(name="backfill-sessions")(self._p.cli_errors(self._backfill))

    def _optimize(
        self,
        force: Annotated[
            bool, typer.Option("--force", help="Bypass the fragment-count guard")
        ] = False,
    ) -> None:
        """Compact the LanceDB table and rebuild indexes (dispatch only).

        The daemon owns the fragment-count safety guard; ``--force`` bypasses it
        for manual recovery.  Returns the task id; poll it with ``quarry status``.
        """
        accepted = self._p.client().optimize(OptimizeRequest(force=force))
        self._p.emit(
            accepted.model_dump(),
            f"Optimize {accepted.status}: task_id={accepted.task_id}",
        )

    def _backfill(
        self,
        dry_run: Annotated[
            bool, typer.Option("--dry-run", help="Scan and report without writing")
        ] = False,
        collection: Annotated[
            str, typer.Option("--collection", "-c", help="Override target collection")
        ] = "",
        project: Annotated[
            str, typer.Option("--project", help="Only backfill this project path")
        ] = "",
        limit: Annotated[
            int, typer.Option("--limit", "-n", help="Max transcripts to process")
        ] = 0,
    ) -> None:
        """Backfill historical Claude Code session transcripts (dispatch only).

        Scans ``~/.claude/projects/`` for JSONL transcripts and ingests them into
        per-project capture collections based on quarry registrations.
        """
        req = BackfillRequest(
            dry_run=dry_run, collection=collection, project=project, limit=limit
        )
        accepted = self._p.client().backfill_sessions(req)
        self._p.emit(
            accepted.model_dump(),
            f"Backfill {accepted.status}: task_id={accepted.task_id}",
        )
