"""The ``quarry optimize`` and ``quarry backfill-sessions`` maintenance commands.

Both fire a singleton 202 task on the daemon and wait for its terminal result, so
the emitted fields match what the local path produced (bug-class-3 parity).  A
concurrent run is rejected by the daemon with 409, which the shared error
decorator maps to exit 0 ("already in progress").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.api import BackfillRequest, OptimizeRequest

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing
    from quarry.client import TaskOutcome


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
        """Compact the LanceDB table and rebuild indexes.

        The daemon owns the fragment-count safety guard; ``--force`` bypasses it
        for manual recovery.  A skip (guard tripped) exits non-zero with the
        reason, never a false success.
        """
        client = self._p.client()
        accepted = client.optimize(OptimizeRequest(force=force))
        outcome = client.await_task(accepted.task_id)
        self._require_completed(outcome, "Optimize")
        results = outcome.results
        if not results.get("optimized"):
            reason = results.get("reason") or "fragment-count guard"
            self._p.err_console.print(f"Skipping: {reason}.", style="yellow")
            raise typer.Exit(code=1)
        self._p.emit(
            {
                "optimized": True,
                "fragments_before": outcome.result_int("fragments_before"),
                "force": force,
            },
            "Optimization complete.",
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
        """Backfill historical Claude Code session transcripts.

        Scans ``~/.claude/projects/`` for JSONL transcripts and ingests them into
        per-project capture collections based on quarry registrations.
        """
        client = self._p.client()
        req = BackfillRequest(
            dry_run=dry_run, collection=collection, project=project, limit=limit
        )
        outcome = client.await_task(client.backfill_sessions(req).task_id)
        self._require_completed(outcome, "Backfill")
        self._p.emit(dict(outcome.results), self._render(outcome, dry_run=dry_run))

    def _require_completed(self, outcome: TaskOutcome, label: str) -> None:
        """Fail 1 when the task did not reach a completed terminal state."""
        if not outcome.is_completed:
            self._p.err_console.print(
                f"{label} did not complete: {outcome.error or outcome.status}",
                style="red",
            )
            raise typer.Exit(code=1)

    @staticmethod
    def _render(outcome: TaskOutcome, *, dry_run: bool) -> str:
        """Render the backfill summary line from the completed task result."""
        ingested = outcome.result_int("ingested")
        present = outcome.result_int("skipped_existing")
        unregistered = outcome.result_int("skipped_unregistered")
        if dry_run:
            return (
                f"[DRY RUN] Would ingest {ingested} transcripts "
                f"({present} already present, {unregistered} unregistered)"
            )
        text = (
            f"Backfill complete: {ingested} ingested, {present} skipped "
            f"(already present), {unregistered} skipped (unregistered)"
        )
        empty = outcome.result_int("skipped_empty")
        if empty:
            text += f", {empty} skipped (empty)"
        errors = outcome.results.get("errors")
        if isinstance(errors, list) and errors:
            text += f", {len(errors)} errors"
        return text
