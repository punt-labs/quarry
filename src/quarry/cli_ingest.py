"""The ``quarry ingest`` and ``quarry remember`` commands: content ingestion.

Both are pure client calls returning the daemon's 202 ``task_id``.  ``ingest``
accepts a URL only — local files and directories are covered by
``quarry register`` + sync, so a non-URL source is rejected with that pointer
rather than an in-process file loader (there is no engine in the CLI).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.api import IngestRequest, RememberRequest

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing


@final
class IngestCli:
    """Register and serve ``ingest``/``remember`` around an injected plumbing bundle."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach the ``ingest`` and ``remember`` commands to *app*."""
        app.command(name="ingest")(self._p.cli_errors(self._ingest))
        app.command(name="remember")(self._p.cli_errors(self._remember))

    def _ingest(
        self,
        source: Annotated[str, typer.Argument(help="URL to ingest (http/https)")],
        overwrite: Annotated[
            bool, typer.Option("--overwrite", help="Replace existing data")
        ] = False,
        collection: Annotated[
            str, typer.Option("--collection", "-c", help="Collection name")
        ] = "",
        agent_handle: Annotated[
            str, typer.Option("--agent-handle", help="Agent handle to tag content")
        ] = "",
        memory_type: Annotated[
            str,
            typer.Option(
                "--memory-type", help="Memory type: fact, observation, opinion"
            ),
        ] = "",
        summary: Annotated[
            str, typer.Option("--summary", help="One-line summary of the content")
        ] = "",
    ) -> None:
        """Ingest a URL into the knowledge base.

        Fetches an ``http(s)`` URL with sitemap discovery and single-page
        fallback.  For local files and directories, use ``quarry register <dir>``
        and let sync index them.
        """
        if not source.startswith(("http://", "https://")):
            self._p.err_console.print(
                f"Error: {source!r} is not a URL. Use 'quarry register <dir>' to "
                "track local files and directories, then 'quarry sync'.",
                style="red",
            )
            raise typer.Exit(code=1)
        req = IngestRequest(
            source=source,
            overwrite=overwrite,
            collection=collection,
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
        )
        accepted = self._p.client().ingest_url(req)
        self._p.emit(
            accepted.model_dump(),
            f"Ingest {accepted.status}: task_id={accepted.task_id}",
        )

    def _remember(
        self,
        name: Annotated[
            str, typer.Option("--name", "-n", help="Document name (required)")
        ] = "",
        collection: Annotated[
            str, typer.Option("--collection", "-c", help="Collection name")
        ] = "default",
        format_hint: Annotated[
            str,
            typer.Option("--format", help="Format hint: auto, plain, markdown, latex"),
        ] = "auto",
        overwrite: Annotated[
            bool,
            typer.Option(
                "--overwrite/--no-overwrite",
                help="Replace existing document with same name",
            ),
        ] = True,
        agent_handle: Annotated[
            str, typer.Option("--agent-handle", help="Agent handle to tag content")
        ] = "",
        memory_type: Annotated[
            str,
            typer.Option(
                "--memory-type", help="Memory type: fact, observation, opinion"
            ),
        ] = "",
        summary: Annotated[
            str, typer.Option("--summary", help="One-line summary of the content")
        ] = "",
    ) -> None:
        """Ingest inline content from stdin.

        Reads text from stdin and indexes it. Requires --name to set the document
        name. Overwrites by default; use --no-overwrite to skip an existing doc.
        """
        if not name:
            self._p.err_console.print(
                "Error: --name is required for remember.", style="red"
            )
            raise typer.Exit(code=1)
        content = sys.stdin.read()
        if not content.strip():
            self._p.err_console.print("Error: no content on stdin.", style="red")
            raise typer.Exit(code=1)
        req = RememberRequest(
            name=name,
            content=content,
            collection=collection,
            format_hint=format_hint,
            overwrite=overwrite,
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
        )
        accepted = self._p.client().remember(req)
        self._p.emit(
            accepted.model_dump(),
            f"Remember {accepted.status}: task_id={accepted.task_id}",
        )
