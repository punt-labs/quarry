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
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from quarry.daemon.captured_body_ingest import CapturedBodyIngest
from quarry.daemon.job_spool import SpoolRecord
from quarry.daemon.refusal_aware_terminal import RefusalAwareTerminal
from quarry.daemon.store_run import StoreRun

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_labels import IngestLabels
    from quarry.daemon.tasks import TaskState
    from quarry.http_url import HttpUrl
    from quarry.ingestion.web_ingest import FormatHint

logger = logging.getLogger(__name__)


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
    labels: IngestLabels
    format_hint: FormatHint
    scrub_label: str
    # True only for a capture, whose name is a fetched URL's storage name; a
    # ``remember`` or ``learn`` name is a label, whatever its format hint.
    url_sourced: bool = False
    # When the hook read the snapshot this stores; ``None`` is the queue's own time.
    captured_at: datetime | None = None
    # Hears each progress line of the ingest; ``None`` hears none (the default).
    checkpoint: Callable[[str], None] | None = None

    @property
    def collection(self) -> str:
        """Return the collection this job writes (the queue routing key)."""
        return self.labels.collection

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Scrub then ingest the content in a background thread, tracking state."""
        terminal = RefusalAwareTerminal.for_job(state, self._stored_name)
        with terminal.record():
            store = StoreRun.start(
                self,
                state,
                partial(self.scrub_and_ingest, ctx),
                RefusalAwareTerminal.settlement,
            )
            terminal.apply(await store.wait())

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
        name the pipeline stores (:meth:`InlineIngest.stored_name`), computed once,
        and a skip reports the name the store holds, never the name as sent.
        """
        from quarry.ingestion.ingest_context import Progress  # noqa: PLC0415
        from quarry.ingestion.web_ingest import (  # noqa: PLC0415
            InlineIngest,
            ingest_content,
        )

        inline = InlineIngest(
            self.content,
            format_hint=self.format_hint,
            content_scrubber=self._scrubbed,
            url_sourced=self.url_sourced,
        )
        if existing := self.labels.existing(
            ctx, self._stored_name(), url_sourced=self.url_sourced
        ):
            return dict(existing.skip())
        return dict(
            ingest_content(
                inline,
                self.name,
                Progress(self.checkpoint),
                replace(self.labels.context(ctx), ingested_at=self.captured_at),
            )
        )

    def scrubbed_name(self) -> str:
        """Return the document name as stored, safe to log."""
        return self._stored_name()

    def _stored_name(self) -> str:
        """Return the one derivation the store uses, so a refusal, a re-fetch and a
        skip all name the same document."""
        return self.labels.stored_name(
            self.name, self._scrubbed, url_sourced=self.url_sourced
        )


@dataclass(frozen=True, slots=True)
class CaptureIngestJob:
    """A web-fetch capture: scrub the fetched HTML inline, re-fetch if it's empty.

    The daemon scrubs and stores the already-fetched HTML through the composed
    :class:`ScrubbedIngestJob`.  A JS-rendered or otherwise text-empty page can
    extract to zero chunks; rather than silently index nothing, the daemon then
    re-fetches the *source URL* server-side (scrub on, same captures collection)
    so the page is captured instead of dropped.  That re-fetch is an SSRF sink,
    so the capture route admits ``source_url`` through ``UrlAdmission``
    (``_admit_url``) and carries an ``HttpUrl``; the job does not re-check.  A
    capture with no source URL (a compaction transcript) simply stores what it
    has.  The re-fetch scrubs content and summary, matching the inline phase.
    """

    inline: ScrubbedIngestJob
    source_url: HttpUrl | None  # None: a transcript, nothing to re-fetch

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
        terminal = RefusalAwareTerminal.for_job(state, self.inline.scrubbed_name)
        with terminal.record():
            store = StoreRun.start(
                self,
                state,
                partial(self._capture, ctx),
                RefusalAwareTerminal.settlement,
            )
            terminal.apply(await store.wait())

    def _capture(self, ctx: DaemonContext) -> dict[str, object]:
        """Scrub-ingest inline; on an empty extraction with a source URL, re-fetch.

        A create-if-absent skip also reports ``chunks: 0`` but is not empty —
        the document is already stored under this name — so it returns as a
        skip rather than spending a network round trip to re-fetch a page the
        daemon holds (and then skip or rewrite it a second time).
        """
        result = self.inline.scrub_and_ingest(ctx)
        if RefusalAwareTerminal.is_stored(result) or self.source_url is None:
            return result
        return self._refetch(ctx, self.source_url)

    def _refetch(self, ctx: DaemonContext, source: HttpUrl) -> dict[str, object]:
        """Re-fetch the source URL and store the body by its media type.

        Runs only when the inline phase stored nothing, so a refused media type,
        a binary body, or a failed fetch raises its URL-free reason and the task
        ends ``failed``: reporting ``completed`` would claim a capture that was
        never stored; so does a re-fetch that also extracts no text.  A
        successful fetch flows through the same HTML-vs-text routing the primary
        URL path uses (:class:`CapturedBodyIngest`), so both agree on the
        media-type contract instead of drifting.
        """
        logger.info(
            "capture: %s inline extracted to zero chunks — re-fetching via daemon",
            self.inline.scrubbed_name(),
        )
        return CapturedBodyIngest(source, self.inline.labels).run(ctx)
