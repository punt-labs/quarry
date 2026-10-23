"""Non-file bulk ingest: safe entry selection, options, and threaded fan-out."""

from __future__ import annotations

import logging
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, final

from quarry.failure_reason import FailureReason
from quarry.http_url import HttpUrl
from quarry.ingestion.ingest_context import Progress
from quarry.ingestion.page_report import PageReport
from quarry.ingestion.web_ingest import UrlIngest

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.ingestion.ingest_context import IngestContext
    from quarry.results import IngestResult

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class BulkOptions:
    """Non-context knobs shared by every bulk URL ingest path."""

    workers: int = 4
    delay: float = 0.5
    timeout: int = 30
    include: list[str] | None = None
    exclude: list[str] | None = None
    limit: int = 0

    def __post_init__(self) -> None:
        """Clamp workers to at least 1 — a thread pool needs at least one worker.

        Frozen dataclasses need ``object.__setattr__`` to fix up a field after
        construction; this is the one write that bypasses immutability, and
        only to enforce the invariant, not to change caller-visible state
        after the fact.
        """
        if self.workers < 1:
            object.__setattr__(self, "workers", 1)


@final
@dataclass(frozen=True, slots=True)
class BulkIngestRunner:
    """Fans out ``ingest_one`` over a thread pool and aggregates outcomes."""

    ingest_one: Callable[[UrlIngest, Progress, IngestContext], IngestResult]
    workers: int = 4

    def run(
        self,
        to_ingest: list[tuple[HttpUrl, str | None]],
        progress: Progress,
        context: IngestContext,
        options: BulkOptions,
    ) -> tuple[int, int, list[str], int]:
        """Return ``(ingested, failed, error_messages, empty)``.

        A page counts as ingested only if it stored chunks; one that extracted to
        nothing is *empty*, so a crawl of blank pages cannot read as a success.

        Raises:
            UnindexableContentError: Every page failed, so the crawl indexed
                nothing; the reason names no URL.
        """
        counts: Counter[str] = Counter()
        errors: list[str] = []
        reasons: list[str] = []

        if to_ingest:
            # Always replace existing chunks for URLs that passed dedup — the
            # dedup already skipped unchanged URLs, so every submitted entry is
            # a genuine (re)ingest.
            bulk_context = replace(context, overwrite=True)
            # Workers report through a callback-less Progress: the parallel ingest_url
            # calls never forward progress_callback (only this method's own
            # aggregation messages below use the caller's callback), and
            # Progress.__call__ invokes that callback with no lock — sharing one
            # callback-bearing Progress across threads would be a new concurrency
            # hazard for a stateful callback. Every worker message still reaches
            # the logger.
            worker_progress = Progress(None)
            with ThreadPoolExecutor(max_workers=self.workers) as executor:
                futures = {
                    executor.submit(
                        self.ingest_one,
                        UrlIngest(
                            url=page_url,
                            document_name=doc_name,
                            timeout=options.timeout,
                            delay=options.delay,
                        ),
                        worker_progress,
                        bulk_context,
                    ): page_url
                    for page_url, doc_name in to_ingest
                }
                for future in as_completed(futures):
                    page_url = futures[future]
                    # PageReport names the page by the URL's log form, so no sink
                    # below sees a path, query or fragment (CWE-532).
                    report = PageReport(page_url)
                    try:
                        outcome = self._outcome(future)
                        counts[outcome] += 1
                        progress(
                            "%s %s (%d/%d)",
                            outcome.capitalize(),
                            report.url,
                            counts["ingested"] + counts["empty"],
                            len(to_ingest),
                        )
                    except Exception as exc:  # noqa: BLE001
                        # Broad except is correct here: this is the per-task
                        # outcome boundary for the parallel fan-out (PY-EH-6),
                        # not internal code -- one worker's failure must not
                        # abort the others.  PageReport logs the redacted URL
                        # (a programming error loudly) and renders a URL-free line.
                        counts["failed"] += 1
                        reasons.append(FailureReason.of(exc))
                        errors.append(report.failure(exc))
                        progress("Failed %s", errors[-1])
            PageReport.require_progress(counts["ingested"], counts["failed"], reasons)

        return counts["ingested"], counts["failed"], errors, counts["empty"]

    @staticmethod
    def _outcome(future: Future[IngestResult]) -> str:
        """Return ``"ingested"`` if the page stored chunks, else ``"empty"``."""
        return "ingested" if future.result().get("chunks") else "empty"
