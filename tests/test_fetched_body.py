"""Tests for quarry.ingestion.fetched_body: the fetch gate and the fetched body."""

from __future__ import annotations

import io
import logging
import socket
from http.client import (
    BadStatusLine,
    HTTPMessage,
    IncompleteRead,
    InvalidURL,
    LineTooLong,
)
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

import pytest

from quarry.content_guard import UnindexableContentError
from quarry.ingestion.fetched_body import FetchedBody, FetchGate, FetchRefusedError
from quarry.ingestion.media_type_policy import MediaTypePolicy, UnsupportedContentError
from quarry.ingestion.ssrf_redirect import RedirectRejectedError
from quarry.url_safety import UrlRejectedError


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

    def test_a_connect_time_policy_rejection_keeps_its_rule(self) -> None:
        exc = UrlRejectedError("host 'x' resolves to blocked address 10.0.0.1")
        assert (
            str(FetchRefusedError.from_transfer_failure(exc)) == f"URL rejected: {exc}"
        )

    def test_a_refused_redirect_hop_keeps_its_rule(self) -> None:
        exc = RedirectRejectedError("redirect target rejected: private address")
        assert str(FetchRefusedError.from_transfer_failure(exc)) == str(exc)

    def test_an_http_error_reports_the_status_and_is_closed(self) -> None:
        fp = io.BytesIO(b"")
        exc = HTTPError("http://u:p@h/?t=1", 503, "no", HTTPMessage(), fp)
        assert (
            str(FetchRefusedError.from_transfer_failure(exc))
            == "fetch failed: HTTP 503"
        )
        assert fp.closed

    def test_timeouts_and_unreachable_hosts_are_told_apart(self) -> None:
        assert (
            str(FetchRefusedError.from_transfer_failure(TimeoutError("x")))
            == "fetch failed: timed out"
        )
        unreachable = URLError("dns http://h/?t=1")
        assert str(FetchRefusedError.from_transfer_failure(unreachable)) == (
            "fetch failed: cannot reach the host (str)"
        )

    @pytest.mark.parametrize(
        "exc", [OSError("Cannot reach http://u:p@h/?t=1"), ValueError("bad http://h")]
    )
    def test_any_other_failure_is_reported_by_class_alone(self, exc: Exception) -> None:
        assert (
            str(FetchRefusedError.from_transfer_failure(exc))
            == f"fetch failed: {type(exc).__name__}"
        )

    def test_the_unreachable_reason_class_is_named(self) -> None:
        exc = URLError(socket.gaierror("Name or service not known"))
        assert str(FetchRefusedError.from_transfer_failure(exc)) == (
            "fetch failed: cannot reach the host (gaierror)"
        )

    def test_a_url_error_wrapping_a_timeout_is_a_timeout(self) -> None:
        exc = URLError(TimeoutError("timed out"))
        assert str(FetchRefusedError.from_transfer_failure(exc)) == (
            "fetch failed: timed out"
        )

    @pytest.mark.parametrize(
        "exc",
        [
            InvalidURL("URL can't contain control characters. '/p?token=abc def'"),
            BadStatusLine("token=abc"),
            LineTooLong("header line"),
            IncompleteRead(b"x", 5),
        ],
        ids=["invalid-url", "bad-status", "line-too-long", "incomplete-read"],
    )
    def test_http_client_errors_are_reported_by_class_alone(
        self, exc: Exception
    ) -> None:
        reason = str(FetchRefusedError.from_transfer_failure(exc))
        assert reason == f"fetch failed: {type(exc).__name__}"
        assert "token" not in reason

    def test_an_unclassified_failure_logs_a_message_free_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        try:
            msg = "boom http://u:p@h/?token=SECRET"
            raise RuntimeError(msg)
        except RuntimeError as exc:
            with caplog.at_level(logging.DEBUG):
                FetchRefusedError.from_transfer_failure(exc)
        assert "RuntimeError" in caplog.text
        assert "test_fetched_body" in caplog.text
        assert "SECRET" not in caplog.text

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

    def test_a_raw_header_is_normalized(self) -> None:
        body = FetchedBody("hi", "Application/JSON; charset=utf-8")
        assert (body.text, body.media_type) == ("hi", "application/json")

    def test_label_is_safe_to_show(self) -> None:
        assert FetchedBody("x", "text/plain").label == "text/plain"
        assert FetchedBody("x", "text/plain\n<!-- x -->").label == "(invalid)"
