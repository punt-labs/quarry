"""The daemon's URL-ingestion job: fetch a URL and index it.

The request is validated into an immutable *job* value object that owns its own
background execution, so the validated fields travel together instead of as a
ten-argument task function.  The URL route builds :class:`IngestJob`; the
content front doors build the jobs in :mod:`quarry.daemon.content_jobs`, whose
capture job re-fetches through :class:`CapturedBodyIngest`, as the scrubbed branch here
does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool

from quarry.daemon.captured_body_ingest import CapturedBodyIngest, IngestLabels
from quarry.daemon.job_spool import SpoolRecord
from quarry.daemon.refusal_aware_terminal import RefusalAwareTerminal

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.tasks import TaskState
    from quarry.http_url import HttpUrl


@dataclass(frozen=True, slots=True)
class IngestJob:
    """A validated ingest request that fetches and indexes a URL.

    ``scrub`` set marks a web-fetch capture re-fetch (the hook's fallback): the
    URL is fetched once through the SSRF-checked path, scrubbed, and stored in
    the ``<repo>-captures`` collection the route already resolved into
    ``collection`` (``default-captures`` when the working directory is
    unregistered) — never a sitemap crawl.  ``scrub`` unset is a plain
    ``quarry ingest``: sitemap-aware and unscrubbed, since a deliberately
    ingested document is stored byte-for-byte.  ``collection`` is the routing key
    the queue serializes on, so the route resolves it before the job is built.

    A single URL whose fetch fails or is refused, whose media type is not text,
    or whose body classifies as binary ends the task ``failed`` with a URL-free
    reason and one WARN naming the redacted URL; nothing is stored.  A sitemap
    crawl records each failed page and indexes the rest, and fails only when the
    sitemap itself cannot be read or every page failed.
    """

    source: HttpUrl
    labels: IngestLabels
    scrub: bool

    @property
    def collection(self) -> str:
        """Return the collection this job writes (the queue routing key)."""
        return self.labels.collection

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Fetch and index the URL in a background thread, updating task state."""
        terminal = RefusalAwareTerminal(state, self._captured().subject)
        with terminal.record():
            terminal.complete(await run_in_threadpool(self._ingest, ctx))

    def spool_record(self) -> SpoolRecord | None:
        """Return a snapshot of the source URL; an ``ingest`` has no client copy.

        The source is the recoverable unit — re-issuing the ingest re-fetches it.
        It is spooled in its stored form, never verbatim: the spool is on disk, so
        a query secret is kept only as its digest and userinfo and fragment are
        dropped.  A URL that carried a query must be re-issued by the operator.
        """
        return SpoolRecord(
            kind="ingest",
            collection=self.collection,
            name=self.source.document_name,
            payload=self.source.document_name,
        )

    def _captured(self) -> CapturedBodyIngest:
        return CapturedBodyIngest(self.source, self.labels)

    def _ingest(self, ctx: DaemonContext) -> dict[str, object]:
        """Run the capture re-fetch (scrubbed, captures collection) or plain ingest.

        The scrubbed branch fetches via :meth:`WebFetcher.fetch_body` so a
        JSON/plain-text/XML URL (a REST endpoint, a raw log) is captured as text
        rather than refused by the HTML-only :meth:`WebFetcher.fetch`.  Both
        branches fail the same way: a fetch that fails or is refused, a refused
        media type, or a binary body raises :class:`UnindexableContentError` with
        a URL-free reason (the network libraries quote the raw URL, so the fetch
        layer replaces their messages before they can reach a log or a task).
        """
        if self.scrub:
            return self._captured().run(ctx)
        return self._ingest_plain(ctx)

    def _ingest_plain(self, ctx: DaemonContext) -> dict[str, object]:
        """Run a plain, unscrubbed ``quarry ingest``: sitemap-aware, byte-for-byte."""
        from quarry.ingestion.bulk_ingest import BulkOptions  # noqa: PLC0415
        from quarry.ingestion.ingest_context import Progress  # noqa: PLC0415
        from quarry.ingestion.sitemap_ingest import ingest_auto  # noqa: PLC0415

        return dict(
            ingest_auto(
                self.source,
                Progress(None),
                self.labels.context(ctx),
                BulkOptions(),
            )
        )
