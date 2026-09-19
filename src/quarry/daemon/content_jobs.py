"""The daemon's content jobs: scrubbed inline text, and the capture built on it.

Both content front doors — ``remember`` (and ``learn``) and ``capture`` — build
one :class:`ScrubbedIngestJob`; the capture route wraps it in a
:class:`CaptureIngestJob` that re-fetches the source URL when the inline HTML
extracts to nothing.  The inline job owns the two write-mode semantics of
``overwrite``: ``True`` replaces the document; ``False`` is create-if-absent,
decided on the collection's serialized writer so that two clients that each
saw the name absent cannot both land.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from starlette.concurrency import run_in_threadpool

from quarry.daemon.ingest_jobs import IngestJob
from quarry.daemon.job_spool import SpoolRecord
from quarry.daemon.tasks import task_terminal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.tasks import TaskState
    from quarry.ingestion.ingest_context import MemoryType
    from quarry.ingestion.web_ingest import FormatHint

logger = logging.getLogger(__name__)

# The result key a create-if-absent skip carries.  It is the one way to tell a
# skip (``chunks: 0`` because the document is already stored) from an empty
# extraction (``chunks: 0`` because the page yielded nothing).
_SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ScrubbedIngestJob:
    """A validated inline-ingest request that always scrubs before storing.

    The scrub runs on the worker thread inside ``run_in_threadpool``, never on
    the event loop, so its regex passes do not stall other requests.  Scrubbing
    precedes embedding and storage, so a scrub that raises aborts the whole
    operation before a single chunk is written — a failed scrub leaves nothing
    half-redacted in the database.  The free-form metadata (document name and
    summary) is scrubbed too, but at the choke point: passing ``content_scrubber``
    to ``ingest_content`` is the signal that redacts content AND metadata, so
    this job forwards the raw name/summary and lets the pipeline redact once —
    no surface can forget it (the stdio MCP remember once did).

    ``overwrite=False`` is create-if-absent, not append: a document already
    stored under the (scrubbed) name in the target collection is left
    untouched and the job completes as a skip.  The check runs where it is
    race-free — on the collection's single FIFO writer (``daemon.ingest_queue``),
    after every earlier write to that collection has landed — which is the
    guarantee a client's own check-then-write cannot give, since it reads before
    a racing writer's 202 has been indexed.
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
            result = await run_in_threadpool(self.scrub_and_ingest, ctx)
            state.status = "completed"
            state.results = dict(result)

    def spool_record(self) -> SpoolRecord | None:
        """Return a scrubbed snapshot; ``remember`` has no durable client copy.

        The content and name are scrubbed here (the same redaction ``run`` would
        apply at ingest), so a drain-abort snapshot never lands unredacted on
        disk while still carrying the knowledge back for recovery.
        """
        return SpoolRecord(
            kind=self.scrub_label,
            collection=self.collection,
            name=self._scrubbed(self.name),
            payload=self._scrubbed(self.content),
        )

    def _scrubbed(self, text: str) -> str:
        """Redact *text* under this job's scrub label (shared by ingest + spool)."""
        from quarry.scrub import scrub_and_log  # noqa: PLC0415

        return scrub_and_log(text, self.scrub_label)

    def scrub_and_ingest(self, ctx: DaemonContext) -> dict[str, object]:
        """Ingest with a scrubber, or skip an existing document when not overwriting.

        The pipeline redacts content AND metadata.  The existence check uses the
        scrubbed name because that is the name the pipeline stores.
        """
        if not self.overwrite and self._already_stored(ctx):
            return {
                "document_name": self.name,
                "collection": self.collection,
                "chunks": 0,
                _SKIPPED: "exists",
            }
        from quarry.ingestion.ingest_context import (  # noqa: PLC0415
            IngestContext,
            Progress,
        )
        from quarry.ingestion.web_ingest import (  # noqa: PLC0415
            InlineIngest,
            ingest_content,
        )

        return dict(
            ingest_content(
                InlineIngest(
                    self.content,
                    # self.format_hint is a plain str from the CLI/HTTP boundary;
                    # InlineIngest's field is the tightened FormatHint Literal.
                    format_hint=cast("FormatHint", self.format_hint),
                    content_scrubber=self._scrubbed,
                ),
                self.name,
                Progress(None),
                IngestContext(
                    ctx.database,
                    ctx.settings,
                    overwrite=self.overwrite,
                    collection=self.collection,
                    agent_handle=self.agent_handle,
                    # self.memory_type is a plain str carried from the daemon's
                    # HTTP boundary (unvalidated beyond the reserved-value
                    # reject); IngestContext's field is the tightened Literal.
                    memory_type=cast("MemoryType", self.memory_type),
                    summary=self.summary,
                ),
            )
        )

    def _already_stored(self, ctx: DaemonContext) -> bool:
        """Return whether the collection already holds a document of this name."""
        return ctx.database.catalog.document_exists(
            self._scrubbed(self.name), self.collection
        )

    @staticmethod
    def is_stored(result: Mapping[str, object]) -> bool:
        """Return whether the document is in the store after *result*.

        True when chunks were written, or when the create-if-absent check found
        it already there (``skipped: "exists"``). Only a result that is neither
        — an extraction that produced nothing — leaves the store without it.
        """
        return bool(result.get("chunks")) or _SKIPPED in result


@dataclass(frozen=True, slots=True)
class CaptureIngestJob:
    """A web-fetch capture: scrub the fetched HTML inline, re-fetch if it's empty.

    The daemon scrubs and stores the already-fetched HTML through the composed
    :class:`ScrubbedIngestJob`.  A JS-rendered or otherwise text-empty page can
    extract to zero chunks; rather than silently index nothing, the daemon then
    re-fetches the *source URL* server-side (scrub on, same captures collection)
    so the page is captured instead of dropped.  That re-fetch is an SSRF sink,
    so the capture route runs the ``UrlSafetyCheck`` gate on ``source_url`` at
    the boundary before building this job — the job trusts an already-validated
    URL and does not re-check.  A capture with no source URL (a compaction
    transcript) simply stores what it has.  The re-fetch scrubs content and
    summary, matching the inline phase.
    """

    inline: ScrubbedIngestJob
    source_url: str

    @property
    def collection(self) -> str:
        """Return the captures collection this job writes (the queue routing key)."""
        return self.inline.collection

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: a capture's transcript ``.md`` predates the POST.

        The client-side artifact already outlives a drain-abort and ``quarry
        backfill`` re-ingests it, so the daemon need not spool this job.
        """
        return None

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Ingest inline, re-fetching the source on empty, tracking task state."""
        with task_terminal(state):
            result = await run_in_threadpool(self._capture, ctx)
            state.status = "completed"
            state.results = dict(result)

    def _capture(self, ctx: DaemonContext) -> dict[str, object]:
        """Scrub-ingest inline; on an empty extraction with a source URL, re-fetch.

        A create-if-absent skip also reports ``chunks: 0`` but is not empty —
        the document is already stored under this name — so it returns as a
        skip rather than spending a network round trip to re-fetch a page the
        daemon holds (and then skip or rewrite it a second time).
        """
        result = self.inline.scrub_and_ingest(ctx)
        if ScrubbedIngestJob.is_stored(result) or not self.source_url:
            return result
        return self._refetch(ctx)

    def _refetch(self, ctx: DaemonContext) -> dict[str, object]:
        """Re-fetch source URL and delegate routing to :class:`IngestJob`.

        A safety/network failure logs cleanly at WARN and returns an empty
        result — never a traceback — because ``_capture`` already stored what
        the inline phase had.  Failure handling delegates to
        :meth:`IngestJob.safe_fetch_body` so both fetch paths share the same
        redacted-log semantics (CWE-532).  A successful fetch flows through
        the same HTML-vs-text routing :class:`IngestJob` uses for the primary
        URL path (:meth:`IngestJob.ingest_captured_body`), so both paths
        agree on the media-type contract instead of drifting.
        """
        body = IngestJob.safe_fetch_body(self.source_url, "capture: refetch")
        if body is None:
            return {"chunks": 0, "sections": 0}

        logger.info(
            "capture: %s inline extracted to zero chunks — re-fetching via daemon (%s)",
            self.inline.name,
            IngestJob.sanitize_media_type(body.media_type),
        )
        return IngestJob(
            source=self.source_url,
            overwrite=self.inline.overwrite,
            collection=self.inline.collection,
            scrub=True,
            agent_handle=self.inline.agent_handle,
            memory_type=self.inline.memory_type,
            summary=self.inline.summary,
            document_name_override=self.inline.name,
        ).ingest_captured_body(ctx, body)
