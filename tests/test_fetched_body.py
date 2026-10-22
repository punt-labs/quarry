"""Tests for quarry.ingestion.fetched_body: the fetch gate and the fetched body."""

from __future__ import annotations

import pytest

from quarry.ingestion.fetched_body import FetchedBody, FetchGate
from quarry.ingestion.media_type_policy import MediaTypePolicy, UnsupportedContentError


class TestFetchGate:
    @pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "x"])
    def test_non_http_urls_are_refused(self, url: str) -> None:
        with pytest.raises(ValueError, match="Only HTTP"):
            FetchGate.check_url(url)

    def test_unsafe_initial_urls_are_refused(self) -> None:
        with pytest.raises(ValueError, match="URL rejected"):
            FetchGate.check_url("http://127.0.0.1/admin")

    def test_unsafe_final_urls_are_refused(self) -> None:
        with pytest.raises(ValueError, match="final URL rejected"):
            FetchGate.check_final_url("http://169.254.169.254/latest")

    def test_media_types_follow_the_policy(self) -> None:
        gate = FetchGate(MediaTypePolicy(exact=frozenset(), suffixes=()))
        gate.check_media_type("text/plain")
        with pytest.raises(UnsupportedContentError):
            gate.check_media_type("application/json")


class TestFetchedBody:
    @pytest.mark.parametrize(
        ("media_type", "is_html"),
        [
            ("", True),
            ("text/html", True),
            ("application/xhtml+xml", True),
            ("text/plain", False),
            ("application/json", False),
        ],
    )
    def test_is_html(self, media_type: str, *, is_html: bool) -> None:
        assert FetchedBody(text="x", media_type=media_type).is_html is is_html
