"""Tests for quarry.ingestion.crawl_guard: a crawl of only unsafe URLs fails."""

from __future__ import annotations

import pytest

from quarry.ingestion.bulk_ingest import BulkOptions
from quarry.ingestion.crawl_guard import CrawlGuard
from quarry.ingestion.fetched_body import FetchRefusedError


def test_every_discovered_url_rejected_is_refused() -> None:
    with pytest.raises(FetchRefusedError) as info:
        CrawlGuard.of(BulkOptions()).require_safe(3, 0)
    assert str(info.value) == "fetch failed: every URL the sitemap lists was rejected"


def test_one_safe_url_is_enough() -> None:
    CrawlGuard.of(BulkOptions()).require_safe(3, 1)


def test_an_empty_sitemap_is_not_this_guards_concern() -> None:
    CrawlGuard.of(BulkOptions()).require_safe(0, 0)


@pytest.mark.parametrize(
    "options",
    [BulkOptions(include=["/docs/*"]), BulkOptions(exclude=["/private/*"])],
    ids=["include", "exclude"],
)
def test_a_restricted_crawl_may_select_nothing(options: BulkOptions) -> None:
    CrawlGuard.of(options).require_safe(3, 0)
