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
            "fetch failed: cannot reach the host"
        )

    @pytest.mark.parametrize(
        "exc",
        [OSError("Cannot reach http://u:p@h/?t=1"), BadStatusLine("token=1")],
        ids=["oserror", "httpexception"],
    )
    def test_any_other_transport_failure_is_reported_by_class_alone(
        self, exc: Exception
    ) -> None:
        assert (
            str(FetchRefusedError.from_transfer_failure(exc))
            == f"fetch failed: {type(exc).__name__}"
        )

    def test_a_plain_value_error_is_not_refusable(self) -> None:
        assert not isinstance(ValueError("bad http://h"), FetchRefusedError.REFUSABLE)

    @pytest.mark.parametrize(
        "exc",
        [UrlRejectedError("blocked"), RedirectRejectedError("redirect target")],
    )
    def test_policy_rejections_are_refusable(self, exc: Exception) -> None:
        assert isinstance(exc, FetchRefusedError.REFUSABLE)

    @pytest.mark.parametrize(
        "exc",
        [
            URLError("dns http://h/?t=SECRET"),
            TimeoutError("slow http://h/?t=SECRET"),
            OSError("reset http://h/?t=SECRET"),
            UrlRejectedError("blocked"),
        ],
        ids=["urlerror", "timeout", "oserror", "policy"],
    )
    def test_every_refusal_logs_one_warning_without_the_message(
        self, exc: Exception, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            FetchRefusedError.from_transfer_failure(exc)
        [record] = caplog.records
        assert record.levelno == logging.WARNING
        assert type(exc).__name__ in record.getMessage()
        assert "SECRET" not in caplog.text

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


class TestSubject:
    def test_the_warning_names_the_redacted_subject_on_the_same_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            FetchRefusedError.from_transfer_failure(
                URLError("dns"), subject="https://h.test/…(segments=2)"
            )
        [record] = caplog.records
        assert "URLError" in record.getMessage()
        assert "https://h.test/…(segments=2)" in record.getMessage()

    def test_a_refusal_without_a_subject_still_logs_one_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            FetchRefusedError.from_transfer_failure(URLError("dns"))
        assert len(caplog.records) == 1


class TestAStatusIsAnAnswerNotAFault:
    @staticmethod
    def _status(code: int) -> HTTPError:
        return HTTPError(
            "http://u:p@h/?t=SECRET", code, "x", HTTPMessage(), io.BytesIO()
        )

    def test_a_404_logs_one_info_line_without_frames(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            FetchRefusedError.from_transfer_failure(
                self._status(404), subject="https://h.test/…(segments=1)"
            )
        [record] = caplog.records
        assert record.levelno == logging.INFO
        assert record.getMessage() == (
            "fetch refused: HTTP 404 on https://h.test/…(segments=1)"
        )
        assert 'File "' not in caplog.text

    def test_a_503_logs_one_warning_without_frames(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            FetchRefusedError.from_transfer_failure(
                self._status(503), subject="https://h.test/…(segments=1)"
            )
        [record] = caplog.records
        assert record.levelno == logging.WARNING
        assert 'File "' not in caplog.text

    def test_a_status_with_no_subject_still_logs_one_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            FetchRefusedError.from_transfer_failure(self._status(404))
        assert [r.getMessage() for r in caplog.records] == ["fetch refused: HTTP 404"]

    def test_a_429_is_a_transient_fault_and_logs_a_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            FetchRefusedError.from_transfer_failure(self._status(429), subject="s")
        assert [r.levelno for r in caplog.records] == [logging.WARNING]

    @pytest.mark.parametrize("code", [404, 429, 503])
    def test_a_status_the_caller_reports_itself_is_debug(
        self, code: int, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            FetchRefusedError.from_transfer_failure(
                self._status(code), subject="s", reported_by_caller=True
            )
        assert [r.levelno for r in caplog.records] == [logging.DEBUG]

    def test_a_transport_fault_is_a_warning_even_when_statuses_are_reported(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            FetchRefusedError.from_transfer_failure(
                OSError("reset"), subject="s", reported_by_caller=True
            )
        assert [r.levelno for r in caplog.records] == [logging.WARNING]

    def test_a_connection_reset_still_logs_its_frames(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        try:
            raise ConnectionResetError("reset")
        except ConnectionResetError as exc:
            with caplog.at_level(logging.DEBUG):
                FetchRefusedError.from_transfer_failure(exc, subject="s")
        assert 'File "' in caplog.text


class TestAPolicyRefusal:
    def test_it_logs_one_warning_with_the_subject_and_returns_the_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            refusal = FetchRefusedError.policy(
                "SSRF-blocked URL: private address", subject="https://h.test/…"
            )
        assert str(refusal) == "SSRF-blocked URL: private address"
        [record] = caplog.records
        assert record.levelno == logging.WARNING
        assert "https://h.test/…" in record.getMessage()
        assert "private address" in record.getMessage()
