"""Tests for quarry.ingestion.page_report: a crawled page's safe name and failure."""

from __future__ import annotations

import logging
from http.client import BadStatusLine

import pytest

from quarry.content_guard import UnindexableContentError
from quarry.db.row_budget import RowBudgetError
from quarry.http_url import HttpUrl
from quarry.ingestion.page_report import PageReport

_URL = "https://example.com/p?token=SECRET#frag"


def test_the_url_is_redacted() -> None:
    assert PageReport(HttpUrl(_URL)).url == "https://example.com/…(1 segment)"


class TestFailure:
    def test_a_refusal_is_described_by_its_url_free_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        exc = UnindexableContentError("content rejected: unsupported media type x")
        with caplog.at_level(logging.WARNING):
            text = PageReport(HttpUrl(_URL)).failure(exc)
        assert text == (
            "https://example.com/…(1 segment): "
            "content rejected: unsupported media type x"
        )
        [record] = caplog.records
        assert record.levelno == logging.WARNING
        assert record.exc_info is None

    def test_a_row_budget_violation_is_a_refusal_with_its_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        reason = "row budget exceeded: text 70,000 bytes > 65,536"
        with caplog.at_level(logging.WARNING):
            text = PageReport(HttpUrl(_URL)).failure(RowBudgetError(reason))
        assert text == f"https://example.com/…(1 segment): {reason}"
        assert caplog.records[0].levelno == logging.WARNING

    @pytest.mark.parametrize(
        "exc",
        [
            OSError("Cannot reach https://u:p@h/?token=1"),
            BadStatusLine("token=1"),
        ],
    )
    def test_other_expected_failures_name_their_class_alone(
        self, exc: Exception, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            text = PageReport(HttpUrl(_URL)).failure(exc)
        assert text == (
            f"https://example.com/…(1 segment): internal error: {type(exc).__name__}"
        )
        assert caplog.records[0].levelno == logging.WARNING
        assert "token" not in caplog.text

    def test_a_programming_error_is_an_error_with_a_message_free_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        try:
            msg = "boom https://u:p@h/?token=SECRET"
            raise KeyError(msg)
        except KeyError as exc:
            with caplog.at_level(logging.WARNING):
                text = PageReport(HttpUrl(_URL)).failure(exc)
        assert text == "https://example.com/…(1 segment): internal error: KeyError"
        [record] = caplog.records
        assert record.levelno == logging.ERROR
        assert "test_page_report" in record.getMessage()
        assert "SECRET" not in caplog.text


class TestRequireProgress:
    def test_a_crawl_where_every_page_failed_is_refused(self) -> None:
        with pytest.raises(UnindexableContentError) as info:
            PageReport.require_progress(0, 2, ["content rejected: x", "y"])
        assert str(info.value) == (
            "crawl ingested nothing: 2 pages failed (first: content rejected: x)"
        )

    def test_a_partial_crawl_passes(self) -> None:
        PageReport.require_progress(1, 1, ["x"])

    def test_an_empty_crawl_passes(self) -> None:
        PageReport.require_progress(0, 0, [])
