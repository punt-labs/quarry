"""Tests for quarry.ingest_collection — the URL-ingest queue routing key."""

from __future__ import annotations

from quarry.ingest_collection import IngestCollection


def test_explicit_collection_is_kept() -> None:
    """An explicit collection is returned verbatim, never re-derived."""
    assert IngestCollection.resolve("https://example.com/page", "docs").name == "docs"


def test_empty_collection_resolves_to_url_host() -> None:
    """An omitted collection resolves to the URL hostname, not the empty string."""
    assert (
        IngestCollection.resolve("https://example.com/page", "").name == "example.com"
    )


def test_explicit_host_and_empty_resolve_to_the_same_key() -> None:
    """collection=host and omitted-collection for that host share one queue key.

    This is the single-writer invariant (quarry-ickn): both requests must route
    to one FIFO worker for table ``example.com``.
    """
    explicit = IngestCollection.resolve("https://example.com/a", "example.com").name
    derived = IngestCollection.resolve("https://example.com/b", "").name
    assert explicit == derived == "example.com"


def test_hostless_url_falls_back_to_default() -> None:
    """A URL with no hostname resolves to ``default`` rather than an empty key."""
    assert IngestCollection.resolve("not-a-url", "").name == "default"
