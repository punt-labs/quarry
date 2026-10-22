"""Tests for quarry.ingestion.fetch_refused: every fetch failure, URL-free."""

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

import pytest

from quarry.content_guard import UnindexableContentError
from quarry.ingestion.fetch_refused import FetchRefusedError
from quarry.ingestion.ssrf_redirect import RedirectRejectedError
from quarry.url_safety import UrlRejectedError


class TestFromTransferFailure:
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
            with caplog.at_level(logging.WARNING):
                FetchRefusedError.from_transfer_failure(exc)
        assert [r.levelno for r in caplog.records] == [logging.WARNING]
        assert "RuntimeError" in caplog.text
        assert "test_fetch_refused" in caplog.text
        assert "SECRET" not in caplog.text

    def test_refusals_are_unindexable_content(self) -> None:
        assert issubclass(FetchRefusedError, UnindexableContentError)
