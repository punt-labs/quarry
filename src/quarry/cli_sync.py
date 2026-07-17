"""The directory-sync commands: ``sync``, ``register``, ``deregister``, ``status``.

All pure client calls.  ``deregister`` fires the daemon's purge task and waits
for the deleted-chunk count so its ``{collection, removed, deleted_chunks}`` shape
is identical to what the local path emitted (bug-class-3 parity); a 409 "already
in progress" on ``sync`` is mapped to exit 0 by the shared error decorator.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.api import DeregisterRequest, RegisterRequest
from quarry.deregister_result import DeregisterResult
from quarry.formatting import format_status

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing
    from quarry.client import TaskOutcome


@final
class SyncCli:
    """Serve ``sync``/``register``/``deregister``/``status`` on an injected plumbing."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach ``sync``, ``register``, ``deregister``, and ``status`` to *app*."""
        app.command(name="sync")(self._p.cli_errors(self._sync))
        app.command(name="register")(self._p.cli_errors(self._register))
        app.command(name="deregister")(self._p.cli_errors(self._deregister))
        app.command(name="status")(self._p.cli_errors(self._status))

    def _sync(
        self,
        workers: Annotated[
            int | None,
            typer.Option("--workers", "-w", help="Ignored — the daemon sizes workers"),
        ] = None,
    ) -> None:
        """Sync all registered directories: ingest new/changed, remove deleted."""
        if workers is not None and not self._p.is_quiet():
            self._p.err_console.print(
                "Warning: --workers is ignored; the daemon sizes its own workers.",
                style="yellow",
            )
        client = self._p.client()
        accepted = client.sync()
        # Await the terminal state: exiting 0 on the bare 202 would report success
        # while the sync is still running or after it FAILED on the daemon.  A 409
        # "already in progress" raised by sync() is mapped to exit 0 by the decorator.
        outcome = client.await_task(accepted.task_id)
        if not outcome.is_completed:
            self._p.err_console.print(self._incomplete(outcome), style="red")
            raise typer.Exit(code=1)
        self._p.emit(dict(outcome.results), self._render(outcome.results))

    @staticmethod
    def _incomplete(outcome: TaskOutcome) -> str:
        """The stderr message for a sync that did not reach ``completed``.

        A poll timeout is surfaced as "still running", never as success — the
        sync keeps going on the daemon and the caller polls it via status.
        """
        if outcome.status == "timed_out":
            return (
                f"Sync is still running (task_id={outcome.task_id}); it will "
                "finish in the background — run 'quarry status' to check."
            )
        return f"Sync did not complete: {outcome.error or outcome.status}"

    @staticmethod
    def _render(results: Mapping[str, object]) -> str:
        """Render the completed sync's ``{collection: counts}`` as a summary."""
        if not results:
            return "Nothing to sync (no registered directories)."
        lines: list[str] = []
        for col, raw in results.items():
            res = raw if isinstance(raw, Mapping) else {}
            line = (
                f"{col}: {res.get('ingested', 0)} ingested, "
                f"{res.get('refreshed', 0)} refreshed, "
                f"{res.get('deleted', 0)} deleted, "
                f"{res.get('skipped', 0)} unchanged, {res.get('failed', 0)} failed"
            )
            errors = res.get("errors")
            if isinstance(errors, list) and errors:
                line += "\n" + "\n".join(f"  error: {e}" for e in errors)
            lines.append(line)
        return "\n".join(lines)

    def _register(
        self,
        directory: Annotated[Path, typer.Argument(help="Directory to register")],
        collection: Annotated[
            str,
            typer.Option("--collection", "-c", help="Collection name (default: dir)"),
        ] = "",
    ) -> None:
        """Register a directory for incremental sync."""
        # The daemon enforces an absolute path inside its $HOME, so send the
        # resolved path the server will allowlist against.
        resolved = str(directory.expanduser().resolve())
        col = collection or directory.name or Path(resolved).name
        client = self._p.client()
        accepted = client.register(RegisterRequest(directory=resolved, collection=col))
        # Await the terminal state: a bare 202 would report success while the
        # registration was still pending or after it FAILED on the daemon.
        outcome = client.await_task(accepted.task_id)
        if not outcome.is_completed:
            self._p.err_console.print(
                f"Register did not complete: {outcome.error or outcome.status}",
                style="red",
            )
            raise typer.Exit(code=1)
        results = outcome.results
        self._p.emit(
            dict(results),
            f"Registered {results.get('directory', resolved)} as collection "
            f"{results.get('collection', col)!r}",
        )

    def _deregister(
        self,
        collection: Annotated[str, typer.Argument(help="Collection to deregister")],
        keep_data: Annotated[
            bool, typer.Option("--keep-data", help="Keep indexed data in LanceDB")
        ] = False,
    ) -> None:
        """Remove a directory registration. Optionally keep indexed data."""
        client = self._p.client()
        accepted = client.deregister(
            DeregisterRequest(collection=collection, keep_data=keep_data)
        )
        outcome = client.await_task(accepted.task_id)
        if not outcome.is_completed:
            self._p.err_console.print(
                f"Deregister purge did not complete: {outcome.error or outcome.status}"
                f" (the registration was removed; run 'quarry status' to verify).",
                style="red",
            )
            raise typer.Exit(code=1)
        result = DeregisterResult(
            collection, accepted.removed, outcome.result_int("deleted_chunks")
        )
        self._p.emit(
            result.as_dict(),
            f"Deregistered collection {collection!r} "
            f"({result.removed} files, {result.deleted_chunks} chunks deleted)",
        )

    def _status(self) -> None:
        """Show database status: documents, chunks, storage, model info."""
        resp = self._p.client().status()
        data = resp.model_dump()
        self._p.emit(data, format_status(data))
