"""Refuse a crawl whose every discovered URL was rejected as unsafe."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.ingestion.fetch_refused import FetchRefusedError

if TYPE_CHECKING:
    from quarry.ingestion.bulk_ingest import BulkOptions


@final
@dataclass(frozen=True, slots=True)
class CrawlGuard:
    """Tell a sitemap that listed only unsafe URLs from one that listed nothing.

    Dropping an unsafe URL is right, but a sitemap whose every URL was dropped
    must not end ``completed`` with zero pages: that reads as success for a crawl
    that fetched nothing.  When the caller gave include or exclude globs, an
    empty selection may be exactly what was asked for, so only an unrestricted
    crawl is refused.
    """

    restricted: bool

    @classmethod
    def of(cls, options: BulkOptions) -> Self:
        """Return the guard for a crawl run with *options*."""
        return cls(restricted=bool(options.include or options.exclude))

    def require_safe(self, discovered: int, safe: int) -> None:
        """Raise unless a crawl that discovered URLs kept at least one.

        Raises:
            FetchRefusedError: *discovered* is positive and *safe* is zero.
        """
        if discovered and not safe and not self.restricted:
            msg = "fetch failed: every URL the sitemap lists was rejected"
            raise FetchRefusedError(msg)
