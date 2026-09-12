"""Non-file bulk ingest: safe entry selection, options, and threaded fan-out."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, final

from quarry.capture_url import CaptureUrl
from quarry.ingestion.ingest_context import Progress
from quarry.ingestion.web_ingest import UrlIngest

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.ingestion.ingest_context import IngestContext
    from quarry.results import IngestResult
    from quarry.sitemap import SitemapEntry

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
class SafeEntrySelector:
    """Filters sitemap entries to a safe, capped, deduped-against-existing set."""

    include: list[str] | None = None
    exclude: list[str] | None = None
    limit: int = 0

    def select_safe(self, entries: list[SitemapEntry]) -> list[SitemapEntry]:
        """Glob-filter -> SSRF-gate -> cap at ``limit`` SAFE entries (one lazy pass)."""
        from quarry.sitemap import SitemapDiscovery  # noqa: PLC0415

        return SitemapDiscovery.select_safe(
            entries, include=self.include, exclude=self.exclude, limit=self.limit
        )

    def partition(
        self, filtered: list[SitemapEntry], context: IngestContext
    ) -> tuple[list[tuple[str, str | None]], int]:
        """Split filtered entries into ``(to_ingest, skipped_count)`` via dedup.

        Dedup compares each entry's ``<lastmod>`` against the stored ingestion
        timestamp of the same document in *context*'s collection.
        """
        existing_docs = context.database.catalog.list_documents(
            collection_filter=context.collection
        )
        existing_timestamps: dict[str, str] = {
            doc["document_name"]: doc["ingestion_timestamp"] for doc in existing_docs
        }
        to_ingest: list[tuple[str, str | None]] = []
        skipped = 0
        for entry in filtered:
            existing_ts = existing_timestamps.get(entry.loc)
            if (
                existing_ts
                and not context.overwrite
                and entry.lastmod is not None
                and self._entry_is_current(existing_ts, entry.lastmod)
            ):
                skipped += 1
                continue
            to_ingest.append((entry.loc, None))
        return to_ingest, skipped

    @staticmethod
    def _entry_is_current(existing_ts: str, lastmod: datetime) -> bool:
        """Return True if *lastmod* is not newer than the stored *existing_ts*.

        An unparseable stored timestamp returns False so the URL is re-ingested
        rather than silently skipped.
        """
        try:
            existing_dt = datetime.fromisoformat(
                str(existing_ts).replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            logger.warning(
                "sitemap dedup: unparseable stored ingestion_timestamp %r — "
                "forcing re-ingest",
                existing_ts,
            )
            return False
        if existing_dt.tzinfo is None:
            existing_dt = existing_dt.replace(tzinfo=UTC)
        # <lastmod> may omit its UTC offset -- a bare "2026-01-01T00:00:00" is
        # valid sitemap XML -- so lastmod can be timezone-naive even though
        # existing_dt (normalized above) never is. Comparing naive against
        # aware raises TypeError; left uncaught here, that would hit the
        # SAME except above as an unparseable stored timestamp and silently
        # fail-open, forcing re-ingest of every current entry and defeating
        # dedup outright. Assume UTC for a naive lastmod instead, so both
        # sides are always aware before the comparison runs.
        if lastmod.tzinfo is None:
            lastmod = lastmod.replace(tzinfo=UTC)
        return lastmod <= existing_dt


@final
@dataclass(frozen=True, slots=True)
class BulkIngestRunner:
    """Fans out ``ingest_one`` over a thread pool and aggregates outcomes."""

    ingest_one: Callable[[UrlIngest, Progress, IngestContext], IngestResult]
    workers: int = 4

    def run(
        self,
        to_ingest: list[tuple[str, str | None]],
        progress: Progress,
        context: IngestContext,
        options: BulkOptions,
    ) -> tuple[int, int, list[str]]:
        """Return ``(ingested_count, failed_count, error_messages)``."""
        ingested = 0
        failed = 0
        errors: list[str] = []

        if to_ingest:
            # Always replace existing chunks for URLs that passed dedup — the
            # dedup already skipped unchanged URLs, so every submitted entry is
            # a genuine (re)ingest.
            bulk_context = replace(context, overwrite=True)
            # Workers report through a callback-less Progress: the pre-decomposition
            # _bulk_ingest_entries never forwarded progress_callback into the
            # parallel ingest_url calls (only this method's own aggregation
            # messages below used the caller's callback), and Progress.__call__
            # invokes that callback with no lock — sharing one callback-bearing
            # Progress across threads would be a new concurrency hazard for a
            # stateful callback. Every worker message still reaches the logger.
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
                    # page_url is only for the actual submit() call above and the
                    # futures dict key; every progress/log/errors sink below --
                    # success or failure -- must see the redacted form, not the
                    # raw URL, since a userinfo/query secret on a program-
                    # discovered sitemap URL would otherwise leak into
                    # quarry.log or the progress stream (CWE-532).
                    redacted_url = CaptureUrl(page_url).redacted(lambda text: text)
                    try:
                        future.result()
                        ingested += 1
                        progress(
                            "Ingested %s (%d/%d)",
                            redacted_url,
                            ingested,
                            len(to_ingest),
                        )
                    except Exception as exc:  # noqa: BLE001
                        # Broad except is correct here: this is the per-task
                        # outcome boundary for the parallel fan-out (PY-EH-6),
                        # not internal code -- one worker's failure must not
                        # abort the others. But WebFetcher and its callees
                        # also embed the raw URL verbatim in their exception
                        # messages ("Cannot reach {url}: ..."), so str(exc) is
                        # never safe to put in errors[], the progress stream,
                        # or the log either -- report only the exception
                        # CLASS, never its message.
                        failed += 1
                        errors.append(f"{redacted_url}: {type(exc).__name__}")
                        logger.warning(
                            "Failed to ingest %s (%s)", redacted_url, type(exc).__name__
                        )
                        progress("Failed %s: %s", redacted_url, type(exc).__name__)

        return ingested, failed, errors
