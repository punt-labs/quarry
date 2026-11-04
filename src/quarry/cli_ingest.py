"""The ``quarry ingest`` and ``quarry remember`` commands: content ingestion.

Both are pure client calls returning the daemon's 202 ``task_id``.  ``ingest``
accepts a URL only — local files and directories are covered by
``quarry register`` + sync, so a non-URL source is rejected with that pointer
rather than an in-process file loader (there is no engine in the CLI).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, NoReturn, Self, final

import typer

from quarry.api import IngestRequest, RememberRequest
from quarry.cli_ingest_input import (
    AgentHandleOption,
    MemoryTypeOption,
    SummaryOption,
    Utf8Source,
)

if TYPE_CHECKING:
    from quarry.api import TaskAccepted
    from quarry.cli_captures import CliPlumbing


@final
class IngestCli:
    """Register and serve ``ingest``/``remember`` around an injected plumbing bundle."""

    __slots__ = ("_p", "_text")

    _p: CliPlumbing
    _text: Utf8Source

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        self._text = Utf8Source(self._fail)
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach the ``ingest``, ``remember``, and ``learn`` commands to *app*."""
        app.command(name="ingest")(self._p.cli_errors(self._ingest))
        app.command(name="remember")(self._p.cli_errors(self._remember))
        app.command(name="learn")(self._p.cli_errors(self._learn))

    def _ingest(
        self,
        source: Annotated[str, typer.Argument(help="URL to ingest (http/https)")],
        overwrite: Annotated[
            bool, typer.Option("--overwrite", help="Replace a stored URL, else skip it")
        ] = False,
        collection: Annotated[
            str, typer.Option("--collection", "-c", help="Collection name")
        ] = "",
        agent_handle: AgentHandleOption = "",
        memory_type: MemoryTypeOption = "",
        summary: SummaryOption = "",
    ) -> None:
        """Ingest a URL into the knowledge base.

        remember = a specific durable fact, ingest = a URL, learn = a
        distilled lesson that gets retrieval preference.

        Fetches an ``http(s)`` URL with sitemap discovery and single-page
        fallback.  For local files and directories, use ``quarry register <dir>``
        and let sync index them.
        """
        if not source.startswith(("http://", "https://")):
            self._fail(
                f"Error: {source!r} is not a URL. Use 'quarry register <dir>' to "
                "track local files and directories, then 'quarry sync'."
            )
        req = IngestRequest(
            source=source,
            overwrite=overwrite,
            collection=collection,
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
        )
        self._emit_accepted("Ingest", self._p.client().ingest_url(req))

    def _remember(
        self,
        name: Annotated[
            str, typer.Option("--name", "-n", help="Document name (required)")
        ] = "",
        collection: Annotated[
            str,
            typer.Option(
                "--collection",
                "-c",
                help=(
                    "Collection name. Leave empty to route by --agent-handle "
                    "(memory-<handle>) or fall back to 'default'."
                ),
            ),
        ] = "",
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
        agent_handle: AgentHandleOption = "",
        memory_type: MemoryTypeOption = "",
        summary: SummaryOption = "",
    ) -> None:
        """Ingest inline content from stdin.

        remember = a specific durable fact, ingest = a URL, learn = a
        distilled lesson that gets retrieval preference. ``memory_type
        'lesson'`` is reserved for ``quarry learn``; an unknown type is a 400.

        Reads text from stdin and indexes it. Requires --name to set the document
        name. Overwrites by default; use --no-overwrite to skip an existing doc.
        """
        if not name:
            self._fail("Error: --name is required for remember.")
        content = self._text.read_stdin()
        if not content.strip():
            self._fail("Error: no content on stdin.")
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
        self._emit_accepted("Remember", self._p.client().remember(req))

    def _learn(
        self,
        lesson: Annotated[str, typer.Argument(help="The distilled lesson text")],
        topic: Annotated[
            str, typer.Option("--topic", help="Domain tag for this lesson")
        ] = "",
        name: Annotated[
            str,
            typer.Option("--name", "-n", help="User-visible slug for later reference"),
        ] = "",
    ) -> None:
        """Save a distilled lesson that gets retrieval preference.

        remember = a specific durable fact, ingest = a URL, learn = a
        distilled lesson that gets retrieval preference. Lessons are capped
        at 500 characters -- use remember for anything longer.
        """
        self._text.require(lesson, "lesson")
        accepted = self._p.client().learn(lesson, topic=topic, name=name)
        self._emit_accepted("Learn", accepted)

    def _emit_accepted(self, verb: str, accepted: TaskAccepted) -> None:
        """Emit the daemon's 202 acceptance as JSON or a one-line status."""
        self._p.emit(
            accepted.model_dump(),
            f"{verb} {accepted.status}: task_id={accepted.task_id}",
        )

    def _fail(self, message: str) -> NoReturn:
        """Print *message* in red and exit 1 — the caller-error path."""
        self._p.err_console.print(message, style="red")
        raise typer.Exit(code=1)
