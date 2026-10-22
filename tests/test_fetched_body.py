"""Tests for quarry.ingestion.fetched_body: the fetch gate and the fetched body."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from quarry.ingestion.fetch_refused import FetchRefusedError
from quarry.ingestion.fetched_body import FetchedBody, FetchGate
from quarry.ingestion.media_type_policy import MediaTypePolicy, UnsupportedContentError


class _FixedChecker:
    """An injected URL policy: refuses exactly one host, with a fixed reason."""

    def reject_reason(self, url: str) -> str | None:
        return "private address" if urlsplit(url).hostname == "internal.test" else None


class TestFetchGate:
    @pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "x"])
    def test_non_http_urls_are_refused_without_naming_the_url(self, url: str) -> None:
        with pytest.raises(FetchRefusedError) as info:
            FetchGate().check_url(url)
        assert str(info.value) == "URL rejected: only HTTP(S) URLs are supported"

    def test_no_part_of_a_refused_url_is_in_the_message(self) -> None:
        url = "ftp://user:hunter2@example.com/p?token=SECRET#frag"
        with pytest.raises(FetchRefusedError) as info:
            FetchGate().check_url(url)
        for part in ("user", "hunter2", "example.com", "SECRET", "frag"):
            assert part not in str(info.value)

    def test_unsafe_initial_urls_are_refused(self) -> None:
        with pytest.raises(FetchRefusedError, match="URL rejected"):
            FetchGate().check_url("http://127.0.0.1/admin")

    def test_unsafe_final_urls_are_refused(self) -> None:
        with pytest.raises(FetchRefusedError, match="final URL rejected"):
            FetchGate().check_final_url("http://169.254.169.254/latest")

    def test_the_url_policy_is_injectable(self) -> None:
        gate = FetchGate(urls=_FixedChecker())
        gate.check_url("https://public.test/x")
        with pytest.raises(FetchRefusedError, match="URL rejected: private address"):
            gate.check_url("https://internal.test/x")
        with pytest.raises(FetchRefusedError, match="final URL rejected: private"):
            gate.check_final_url("https://internal.test/x")

    def test_a_final_url_is_not_scheme_checked(self) -> None:
        # The opener only follows HTTP(S) hops, so a redirect can newly violate
        # only the address policy.
        FetchGate(urls=_FixedChecker()).check_final_url("ftp://public.test/x")

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

    @pytest.mark.parametrize(
        "header", ["Text/HTML", "text/html; charset=UTF-8", "  TEXT/html  ;x=y"]
    )
    def test_the_media_type_is_normalized_on_construction(self, header: str) -> None:
        assert FetchedBody(text="x", media_type=header).media_type == "text/html"

    def test_a_raw_header_is_normalized(self) -> None:
        body = FetchedBody("hi", "Application/JSON; charset=utf-8")
        assert (body.text, body.media_type) == ("hi", "application/json")

    def test_label_is_safe_to_show(self) -> None:
        assert FetchedBody("x", "text/plain").label == "text/plain"
        assert FetchedBody("x", "text/plain\n<!-- x -->").label == "(invalid)"
