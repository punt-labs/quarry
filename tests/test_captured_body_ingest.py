"""Tests for quarry.daemon.captured_body_ingest: fetch a URL, route its body."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import pytest

from quarry.daemon.captured_body_ingest import CapturedBodyIngest, IngestLabels
from quarry.http_url import HttpUrl
from quarry.ingestion.fetch_refused import FetchRefusedError
from quarry.ingestion.fetched_body import FetchedBody
from quarry.ingestion.web_fetch import WebFetcher

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext

_FETCH_BODY = "quarry.ingestion.web_fetch.WebFetcher.fetch_body"


def _labels() -> IngestLabels:
    return IngestLabels(
        overwrite=True,
        collection="repo-captures",
        agent_handle="",
        memory_type="",
        summary="",
    )


def _captured(
    source: str = "https://x.test/page", name: str = ""
) -> CapturedBodyIngest:
    return CapturedBodyIngest(HttpUrl(source), _labels(), name)


def _ctx() -> DaemonContext:
    return cast(
        "DaemonContext", SimpleNamespace(database=MagicMock(), settings=MagicMock())
    )


class TestFetch:
    def test_returns_the_fetched_body(self) -> None:
        body = FetchedBody(text="hi", media_type="text/plain")
        with patch(_FETCH_BODY, return_value=body):
            assert _captured().fetch() == body

    def test_the_fetcher_is_injectable(self) -> None:
        fetcher = MagicMock(spec=WebFetcher)
        body = FetchedBody(text="hi", media_type="text/plain")
        fetcher.fetch_body.return_value = body
        captured = CapturedBodyIngest(
            HttpUrl("https://x.test/a"), _labels(), fetcher=fetcher
        )
        assert captured.fetch() == body
        fetcher.fetch_body.assert_called_once_with(HttpUrl("https://x.test/a"))

    def test_a_refusal_propagates_untouched(self) -> None:
        refusal = FetchRefusedError("fetch failed: HTTP 500")
        with (
            patch(_FETCH_BODY, side_effect=refusal),
            pytest.raises(FetchRefusedError, match="HTTP 500"),
        ):
            _captured().fetch()


class TestIngest:
    def test_html_is_routed_through_the_extractor_with_the_fetched_text(self) -> None:
        body = FetchedBody(text="<html><body>hi</body></html>", media_type="text/html")
        with patch(
            "quarry.ingestion.web_ingest.ingest_url", return_value={"chunks": 1}
        ) as ingest_url:
            result = _captured().ingest(_ctx(), body)
        assert result == {"chunks": 1}
        assert ingest_url.call_args.args[0].prefetched_html == body.text

    def test_text_is_stored_with_a_media_type_marker_and_the_override_name(
        self,
    ) -> None:
        body = FetchedBody(text="raw log", media_type="text/plain")
        with patch(
            "quarry.ingestion.web_ingest.ingest_content", return_value={"chunks": 1}
        ) as ingest_content:
            _captured(name="WebFetch capture").ingest(_ctx(), body)
        inline, name = ingest_content.call_args.args[:2]
        assert inline.content == "<!-- media_type: text/plain -->\nraw log"
        assert name == "WebFetch capture"

    def test_text_without_an_override_is_named_by_the_redacted_url(self) -> None:
        body = FetchedBody(text="raw log", media_type="text/plain")
        with patch(
            "quarry.ingestion.web_ingest.ingest_content", return_value={"chunks": 1}
        ) as ingest_content:
            _captured("https://x.test/p?token=abc").ingest(_ctx(), body)
        assert ingest_content.call_args.args[1] == "https://x.test/p"

    def test_the_labels_reach_the_pipeline_context(self) -> None:
        body = FetchedBody(text="raw", media_type="text/plain")
        labels = IngestLabels(
            overwrite=False,
            collection="notes",
            agent_handle="rmh",
            memory_type="fact",
            summary="s",
        )
        with patch(
            "quarry.ingestion.web_ingest.ingest_content", return_value={"chunks": 1}
        ) as ingest_content:
            CapturedBodyIngest(HttpUrl("https://x.test/p"), labels).ingest(_ctx(), body)
        context = ingest_content.call_args.args[3]
        assert (context.overwrite, context.collection) == (False, "notes")
        assert (context.agent_handle, context.memory_type) == ("rmh", "fact")


class TestRun:
    def test_a_stored_body_returns_the_ingest_result(self) -> None:
        body = FetchedBody(text="raw log", media_type="text/plain")
        with (
            patch(_FETCH_BODY, return_value=body),
            patch(
                "quarry.ingestion.web_ingest.ingest_content",
                return_value={"chunks": 3},
            ),
        ):
            assert _captured().run(_ctx()) == {"chunks": 3}

    def test_a_failed_fetch_raises_rather_than_returning_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        refusal = FetchRefusedError("fetch failed: timed out")
        with (
            caplog.at_level(logging.DEBUG),
            patch(_FETCH_BODY, side_effect=refusal),
            pytest.raises(FetchRefusedError) as info,
        ):
            _captured().run(_ctx())
        assert info.value is refusal
        assert caplog.records == []


def test_the_subject_drops_userinfo_query_and_fragment() -> None:
    captured = _captured("https://x.test/p?token=abc#f")
    assert captured.subject() == "https://x.test/…(segments=1)"
