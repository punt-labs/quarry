"""Tests for quarry.ingestion.page_report: a crawled page's safe name and failure."""

from __future__ import annotations

import pytest

from quarry.content_guard import UnindexableContentError
from quarry.ingestion.page_report import PageReport


def test_the_url_is_redacted() -> None:
    report = PageReport("https://user:pw@example.com/p?token=SECRET#frag")
    assert report.url == "https://example.com/p"


def test_a_refusal_is_described_by_its_url_free_reason() -> None:
    exc = UnindexableContentError("content rejected: unsupported media type x")
    assert PageReport.failure(exc) == "content rejected: unsupported media type x"


@pytest.mark.parametrize(
    "exc",
    [
        OSError("Cannot reach https://u:p@h/?token=1"),
        ValueError("HTTP 500 for http://h"),
    ],
)
def test_any_other_failure_is_described_by_class_alone(exc: Exception) -> None:
    assert PageReport.failure(exc) == type(exc).__name__
