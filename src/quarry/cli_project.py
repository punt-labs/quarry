"""The ``quarry enable`` / ``quarry disable`` project-capture commands.

``enable`` and ``disable`` do their own local filesystem and registry work
(config.md, CLAUDE.md, ethos ext files) — that is client-side.  The one engine
operation, purging a disabled project's LanceDB chunks, is performed here via a
daemon ``delete_collection`` call rather than an in-process store.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.api import DeleteCollectionRequest

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing
    from quarry.enable import DisableResult


@final
class ProjectCli:
    """Serve ``enable``/``disable`` around an injected plumbing bundle."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach the ``enable`` and ``disable`` commands to *app*."""
        app.command(name="enable")(self._p.cli_errors(self._enable))
        app.command(name="disable")(self._p.cli_errors(self._disable))

    def _enable(
        self,
        directory: Annotated[
            Path, typer.Argument(help="Project directory to enable (default: cwd)")
        ] = Path(),
        collection: Annotated[
            str, typer.Option("--collection", "-c", help="Override collection name")
        ] = "",
    ) -> None:
        """Enable quarry knowledge capture for a project directory."""
        from quarry.enable import enable_project  # noqa: PLC0415

        try:
            result = enable_project(directory.resolve(), collection_override=collection)
        except ValueError as exc:
            self._p.emit({"error": str(exc)}, "")
            self._p.err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from None

        lines = [
            f"Enabled quarry for {result.directory}",
            f"  Collection: {result.collection}",
            f"  Captures: {result.captures_collection}",
        ]
        if result.config_path:
            lines.append(f"  Config: {result.config_path}")
        if result.claudemd_appended:
            lines.append("  Appended quarry instructions to CLAUDE.md")
        if result.ethos_skipped:
            lines.append("  Ethos: not installed (agent memory skipped)")
        else:
            if result.ethos_created:
                lines.append(f"  Ethos created: {', '.join(result.ethos_created)}")
            if result.ethos_updated:
                lines.append(f"  Ethos updated: {', '.join(result.ethos_updated)}")
            if result.memory_collections:
                joined = ", ".join(result.memory_collections)
                lines.append(f"  Memory collections: {joined}")
        self._p.emit(dataclasses.asdict(result), "\n".join(lines))

    def _disable(
        self,
        directory: Annotated[
            Path, typer.Argument(help="Project directory to disable (default: cwd)")
        ] = Path(),
        keep_data: Annotated[
            bool, typer.Option("--keep-data", help="Keep indexed data in LanceDB")
        ] = False,
    ) -> None:
        """Disable quarry knowledge capture for a project directory."""
        from quarry.enable import disable_project  # noqa: PLC0415

        try:
            result = disable_project(directory.resolve(), keep_data=keep_data)
        except ValueError as exc:
            self._p.emit({"error": str(exc)}, "")
            self._p.err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from None

        deleted = self._purge(result)
        data = {
            "directory": result.directory,
            "collection": result.collection,
            "captures_collection": result.captures_collection,
            "deleted_chunks": deleted,
            "config_removed": result.config_removed,
            "claudemd_removed": result.claudemd_removed,
        }
        lines = [f"Disabled quarry for {result.directory}"]
        if deleted > 0:
            lines.append(f"  Deleted {deleted} chunks")
        if result.config_removed:
            lines.append("  Config file removed")
        if result.claudemd_removed:
            lines.append("  Removed quarry instructions from CLAUDE.md")
        self._p.emit(data, "\n".join(lines))

    def _purge(self, result: DisableResult) -> int:
        """Purge each named collection's chunks via the daemon; return the total.

        A purge that fails, times out, or leaves the daemon unreachable must not
        report success while the chunks remain in LanceDB — fail loud (exit 1),
        like the sibling delete/deregister commands, rather than count it as 0.
        """
        client = self._p.client()
        deleted = 0
        for name in result.purge_collections:
            accepted = client.delete_collection(DeleteCollectionRequest(name=name))
            outcome = client.await_task(accepted.task_id)
            if not outcome.is_completed:
                self._p.err_console.print(
                    f"Purge of collection {name!r} did not complete: "
                    f"{outcome.error or outcome.status}",
                    style="red",
                )
                raise typer.Exit(code=1)
            deleted += outcome.result_int("deleted")
        return deleted
