"""The daemon's content-ingestion jobs: scrubbed inline text and fetched URLs.

Each request is validated into an immutable *job* value object that owns its own
background execution, so the validated fields travel together instead of as a
ten-argument task function.  Both the ``remember`` and ``capture`` front doors
build :class:`ScrubbedIngestJob`; the URL route builds :class:`IngestJob`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool

from quarry.daemon.tasks import task_terminal

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.tasks import TaskState


@dataclass(frozen=True, slots=True)
class ScrubbedIngestJob:
    """A validated inline-ingest request that always scrubs before storing.

    The scrub runs on the worker thread inside ``run_in_threadpool``, never on
    the event loop, so its regex passes do not stall other requests.  Scrubbing
    precedes embedding and storage, so a scrub that raises aborts the whole
    operation before a single chunk is written — a failed scrub leaves nothing
    half-redacted in the database.  Free-form metadata (the document name and
    the summary) is scrubbed too: the chunker copies both into every stored
    chunk, so a secret in a remember's name or summary would otherwise leak.
    """

    name: str
    content: str
    collection: str
    format_hint: str
    overwrite: bool
    scrub_label: str
    agent_handle: str
    memory_type: str
    summary: str

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Scrub then ingest the content in a background thread, tracking state."""
        with task_terminal(state):
            result = await run_in_threadpool(self._scrub_and_ingest, ctx)
            state.status = "completed"
            state.results = dict(result)

    def _scrub_and_ingest(self, ctx: DaemonContext) -> dict[str, object]:
        """Scrub the content AND the free-form metadata, then ingest."""
        from quarry.ingestion.pipeline import ingest_content  # noqa: PLC0415
        from quarry.scrub import scrub_and_log  # noqa: PLC0415

        def scrub(text: str) -> str:
            return scrub_and_log(text, self.scrub_label)

        return dict(
            ingest_content(
                self.content,
                scrub(self.name),
                ctx.database,
                ctx.settings,
                overwrite=self.overwrite,
                collection=self.collection,
                format_hint=self.format_hint,
                content_scrubber=scrub,
                agent_handle=self.agent_handle,
                memory_type=self.memory_type,
                summary=scrub(self.summary),
            )
        )


@dataclass(frozen=True, slots=True)
class IngestJob:
    """A validated ingest request that fetches and indexes a URL.

    ``scrub`` set marks a web-fetch capture re-fetch (the hook's fallback): the
    URL is fetched once through the SSRF-checked path, scrubbed, and stored in
    the project's ``<repo>-captures`` collection (``default-captures`` when the
    working directory is unregistered) — never a sitemap crawl.  ``scrub`` unset
    is a plain ``quarry ingest``: sitemap-aware and unscrubbed, since a
    deliberately ingested document is stored byte-for-byte.  Capture-intent is
    carried explicitly here, never inferred from whether ``cwd`` is empty.
    """

    source: str
    overwrite: bool
    collection: str
    cwd: str
    scrub: bool
    agent_handle: str
    memory_type: str
    summary: str

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Fetch and index the URL in a background thread, updating task state."""
        with task_terminal(state):
            result = await run_in_threadpool(self._ingest, ctx)
            state.status = "completed"
            state.results = dict(result)

    def _ingest(self, ctx: DaemonContext) -> dict[str, object]:
        """Run the capture re-fetch (scrubbed, captures collection) or plain ingest."""
        if self.scrub:
            from quarry.captures_collection import CapturesCollection  # noqa: PLC0415
            from quarry.ingestion.pipeline import ingest_url  # noqa: PLC0415
            from quarry.scrub import scrub_and_log  # noqa: PLC0415

            collection = CapturesCollection.for_registry_path(
                self.cwd, ctx.settings.registry_path
            ).name
            return dict(
                ingest_url(
                    self.source,
                    ctx.database,
                    ctx.settings,
                    overwrite=self.overwrite,
                    collection=collection,
                    content_scrubber=lambda text: scrub_and_log(text, "web-fetch"),
                    agent_handle=self.agent_handle,
                    memory_type=self.memory_type,
                    summary=self.summary,
                )
            )

        from quarry.ingestion.pipeline import ingest_auto  # noqa: PLC0415

        return dict(
            ingest_auto(
                self.source,
                ctx.database,
                ctx.settings,
                overwrite=self.overwrite,
                collection=self.collection,
                agent_handle=self.agent_handle,
                memory_type=self.memory_type,
                summary=self.summary,
            )
        )
