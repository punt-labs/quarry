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
    half-redacted in the database.
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
        from quarry.ingestion.pipeline import ingest_content  # noqa: PLC0415
        from quarry.scrub import scrub_and_log  # noqa: PLC0415

        with task_terminal(state):
            result = await run_in_threadpool(
                ingest_content,
                self.content,
                self.name,
                ctx.database,
                ctx.settings,
                overwrite=self.overwrite,
                collection=self.collection,
                format_hint=self.format_hint,
                content_scrubber=lambda text: scrub_and_log(text, self.scrub_label),
                agent_handle=self.agent_handle,
                memory_type=self.memory_type,
                summary=self.summary,
            )
            state.status = "completed"
            state.results = dict(result)


@dataclass(frozen=True, slots=True)
class IngestJob:
    """A validated ingest request that fetches and indexes a URL."""

    source: str
    overwrite: bool
    collection: str
    agent_handle: str
    memory_type: str
    summary: str

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Execute ingest_auto in a background thread and update task state."""
        from quarry.ingestion.pipeline import ingest_auto  # noqa: PLC0415

        with task_terminal(state):
            result = await run_in_threadpool(
                ingest_auto,
                self.source,
                ctx.database,
                ctx.settings,
                overwrite=self.overwrite,
                collection=self.collection,
                agent_handle=self.agent_handle,
                memory_type=self.memory_type,
                summary=self.summary,
            )
            state.status = "completed"
            state.results = dict(result)
