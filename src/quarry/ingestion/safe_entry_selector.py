"""Select the sitemap entries a bulk ingest should fetch: safe, capped, not current."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, final

from quarry.http_url import HttpUrl
from quarry.ingestion.legacy_url_names import LegacyUrlNames

if TYPE_CHECKING:
    from quarry.ingestion.ingest_context import IngestContext
    from quarry.sitemap import SitemapEntry

logger = logging.getLogger(__name__)


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
    ) -> tuple[list[tuple[HttpUrl, str | None]], int]:
        """Split filtered entries into ``(to_ingest, skipped_count)`` via dedup.

        Dedup compares each entry's ``<lastmod>`` against the stored ingestion
        timestamp of the same document in *context*'s collection.  Only a document
        fetched from a URL (one with a source path) counts: a label named like a page
        is not that page.  Entries that store as one name keep the first.
        """
        existing = context.database.catalog.list_documents(
            collection_filter=context.collection
        )
        existing_timestamps = {
            LegacyUrlNames.canonical(doc["document_name"]): doc["ingestion_timestamp"]
            for doc in existing
            if doc["document_path"]  # a label has no source; only a fetched page counts
        }
        by_name: dict[str, tuple[HttpUrl, str | None]] = {}
        skipped = 0
        for entry in filtered:
            url = HttpUrl(entry.loc)
            existing_ts = existing_timestamps.get(url.document_name)
            if (
                existing_ts
                and not context.overwrite
                and entry.lastmod is not None
                and self._entry_is_current(existing_ts, entry.lastmod)
            ):
                skipped += 1
                continue
            by_name.setdefault(url.document_name, (url, None))
        return list(by_name.values()), skipped

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
