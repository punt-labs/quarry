"""Tests for quarry.ingestion.fetched_body: the fetch gate and the fetched body."""

from __future__ import annotations

import io
from http.client import HTTPMessage
from urllib.error import HTTPError, URLError

import pytest

from quarry.content_guard import UnindexableContentError
from quarry.ingestion.fetched_body import FetchedBody, FetchGate, FetchRefusedError
from quarry.ingestion.media_type_policy import MediaTypePolicy, UnsupportedContentError
from quarry.ingestion.ssrf_redirect import RedirectRejectedError
from quarry.url_safety import UrlRejectedError


class _FixedChecker:
    """An injected URL policy: refuses exactly one host, with a fixed reason."""

    def reject_reason(self, url: str) -> str | None:
        return "private address" if "internal.test" in url else None


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

    def test_a_connect_time_policy_rejection_keeps_its_rule(self) -> None:
        exc = UrlRejectedError("host 'x' resolves to blocked address 10.0.0.1")
        assert str(FetchGate.translate(exc)) == f"URL rejected: {exc}"

    def test_a_refused_redirect_hop_keeps_its_rule(self) -> None:
        exc = RedirectRejectedError("redirect target rejected: private address")
        assert str(FetchGate.translate(exc)) == str(exc)

    def test_an_http_error_reports_the_status_and_is_closed(self) -> None:
        fp = io.BytesIO(b"")
        exc = HTTPError("http://u:p@h/?t=1", 503, "no", HTTPMessage(), fp)
        assert str(FetchGate.translate(exc)) == "fetch failed: HTTP 503"
        assert fp.closed

    def test_timeouts_and_unreachable_hosts_are_told_apart(self) -> None:
        assert str(FetchGate.translate(TimeoutError("x"))) == "fetch failed: timed out"
        assert str(FetchGate.translate(URLError("dns http://h/?t=1"))) == (
            "fetch failed: cannot reach the host"
        )

    @pytest.mark.parametrize(
        "exc", [OSError("Cannot reach http://u:p@h/?t=1"), ValueError("bad http://h")]
    )
    def test_any_other_failure_is_reported_by_class_alone(self, exc: Exception) -> None:
        assert str(FetchGate.translate(exc)) == f"fetch failed: {type(exc).__name__}"

    def test_refusals_are_unindexable_content(self) -> None:
        assert issubclass(FetchRefusedError, UnindexableContentError)


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

    def test_from_header_normalizes(self) -> None:
        body = FetchedBody.from_header("hi", "Application/JSON; charset=utf-8")
        assert (body.text, body.media_type) == ("hi", "application/json")

    def test_label_is_safe_to_show(self) -> None:
        assert FetchedBody("x", "text/plain").label == "text/plain"
        assert FetchedBody("x", "text/plain\n<!-- x -->").label == "(invalid)"
