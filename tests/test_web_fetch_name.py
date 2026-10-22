"""Tests for quarry.web_fetch_name: the stored name of a WebFetch capture."""

from __future__ import annotations

from quarry.capture_url import CaptureUrl
from quarry.scrub import scrub_and_log
from quarry.web_fetch_name import WebFetchName


def _scrub(text: str) -> str:
    return scrub_and_log(text, "web-fetch")


def test_matches_manual_redaction() -> None:
    """``WebFetchName.of`` is the one true derivation both write and lookup use."""
    url = "https://x.test/reset?email=user@example.com#frag"
    assert WebFetchName.of(url) == CaptureUrl(url).redacted(_scrub)


def test_query_and_fragment_collapse_to_same_name() -> None:
    """Two URLs differing only by query string or fragment share one document."""
    base = WebFetchName.of("https://x.test/docs/guide")
    with_query = WebFetchName.of("https://x.test/docs/guide?utm=1")
    with_fragment = WebFetchName.of("https://x.test/docs/guide#section")
    assert base == with_query == with_fragment
