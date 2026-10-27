"""Tests for quarry.ingestion.sitemap_node_failure: why a sitemap node failed."""

from __future__ import annotations

import logging

import pytest
from usp.objects.sitemap import InvalidSitemap

from quarry.ingestion.sitemap_node_failure import NodeFailure

_URL = "https://example.com/s.xml?token=SECRET"


def _node(reason: str) -> InvalidSitemap:
    return InvalidSitemap(url=_URL, reason=reason)


class TestClassification:
    def test_a_fetch_failure_is_a_transport_cause_without_the_prefix(self) -> None:
        failure = NodeFailure.of(
            _node(f"Unable to fetch sitemap from {_URL}: HTTP 403")
        )
        assert (failure.kind, failure.cause) == ("transport", "HTTP 403")

    def test_a_leading_fetch_failed_is_not_repeated(self) -> None:
        reason = f"Unable to fetch sitemap from {_URL}: fetch failed: timed out"
        assert NodeFailure.of(_node(reason)).cause == "timed out"

    def test_an_unexpected_error_is_recognised_and_its_text_is_dropped(self) -> None:
        reason = f"Unable to add sub-sitemap from URL {_URL}: boom SECRET"
        failure = NodeFailure.of(_node(reason), bug="ValueError")
        assert (failure.kind, failure.cause) == ("unexpected", "")

    def test_an_add_failure_without_a_recorded_bug_is_a_document_defect(self) -> None:
        reason = f"Unable to add sub-sitemap from URL {_URL}: recursion SECRET"
        failure = NodeFailure.of(_node(reason))
        assert (failure.kind, failure.cause) == ("parse", "")

    @pytest.mark.parametrize(
        "reason",
        [
            f"No parsers support sitemap from {_URL}",
            "Un-fetched child sitemap",
            "Unable to fetch sitemap from https://other.test/?t=SECRET: boom",
            f"Recursion detected when {_URL} redirected to {_URL}",
        ],
    )
    def test_anything_else_is_a_parse_failure_with_no_cause(self, reason: str) -> None:
        failure = NodeFailure.of(_node(reason))
        assert (failure.kind, failure.cause) == ("parse", "")


class TestLogging:
    def test_one_line_per_node_with_the_redacted_url_and_never_the_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        failure = NodeFailure.of(_node(f"No parsers support sitemap from {_URL}"))
        logger = logging.getLogger("t.sitemap")
        with caplog.at_level(logging.DEBUG, logger="t.sitemap"):
            failure.log(logger)
        [record] = [r for r in caplog.records if r.name == "t.sitemap"]
        assert record.levelno == logging.WARNING
        assert "https://example.com/…(segments=1)" in record.getMessage()
        assert "parse failure" in record.getMessage()
        assert "SECRET" not in record.getMessage()
        assert "No parsers" not in record.getMessage()

    def test_a_transport_failure_names_its_cause(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        failure = NodeFailure.of(
            _node(f"Unable to fetch sitemap from {_URL}: HTTP 403")
        )
        with caplog.at_level(logging.DEBUG, logger="t.sitemap"):
            failure.log(logging.getLogger("t.sitemap"))
        assert "HTTP 403" in caplog.text

    def test_an_unexpected_error_is_an_error_record(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        failure = NodeFailure.of(
            _node(f"Unable to add sub-sitemap from URL {_URL}: boom SECRET"),
            bug="ValueError",
        )
        with caplog.at_level(logging.DEBUG, logger="t.sitemap"):
            failure.log(logging.getLogger("t.sitemap"))
        [record] = [r for r in caplog.records if r.name == "t.sitemap"]
        assert record.levelno == logging.ERROR
        assert "SECRET" not in record.getMessage()
        assert "boom" not in record.getMessage()
