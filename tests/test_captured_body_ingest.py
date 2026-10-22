"""Tests for quarry.daemon.captured_body_ingest — fetch a URL, route its body."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import pytest

from quarry.daemon.captured_body_ingest import CapturedBodyIngest
from quarry.ingestion.fetched_body import FetchedBody
from quarry.ingestion.media_type_policy import UnsupportedContentError

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext

_FETCH_BODY = "quarry.ingestion.web_fetch.WebFetcher.fetch_body"


def _captured(
    source: str = "https://x.test/page", name: str = ""
) -> CapturedBodyIngest:
    return CapturedBodyIngest(
        source,
        overwrite=True,
        collection="repo-captures",
        agent_handle="",
        memory_type="",
        summary="",
        name_override=name,
    )


def _ctx() -> DaemonContext:
    return cast(
        "DaemonContext", SimpleNamespace(database=MagicMock(), settings=MagicMock())
    )


class TestFetch:
    def test_returns_the_fetched_body(self) -> None:
        body = FetchedBody(text="hi", media_type="text/plain")
        with patch(_FETCH_BODY, return_value=body):
            assert _captured().fetch("ingest: fetch") == body

    def test_a_refused_media_type_propagates_with_its_reason(self) -> None:
        refusal = UnsupportedContentError("content rejected: unsupported media type x")
        with (
            patch(
                "quarry.ingestion.web_fetch.WebFetcher.fetch_body", side_effect=refusal
            ),
            pytest.raises(UnsupportedContentError, match="content rejected"),
        ):
            _captured().fetch("ingest: fetch")

    @pytest.mark.parametrize(
        "failure",
        [
            OSError("Cannot reach https://u:secret@x.test/p: refused"),
            ValueError("HTTP 500 fetching https://x.test/p?token=abc"),
            TimeoutError("Timed out fetching https://x.test/p?token=abc"),
        ],
    )
    def test_transient_failures_log_only_the_class(
        self, failure: BaseException, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            caplog.at_level(logging.WARNING),
            patch(
                "quarry.ingestion.web_fetch.WebFetcher.fetch_body", side_effect=failure
            ),
        ):
            assert _captured().fetch("ingest: fetch") is None
        assert "secret" not in caplog.text
        assert "token" not in caplog.text
        assert type(failure).__name__ in caplog.text

    def test_policy_rejections_append_their_rule(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        failure = ValueError("URL rejected: private address")
        with (
            caplog.at_level(logging.WARNING),
            patch(
                "quarry.ingestion.web_fetch.WebFetcher.fetch_body", side_effect=failure
            ),
        ):
            assert _captured().fetch("ingest: fetch") is None
        assert "URL rejected: private address" in caplog.text


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
            _captured("https://u:secret@x.test/p?token=abc").ingest(_ctx(), body)
        assert ingest_content.call_args.args[1] == "https://x.test/p"


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
            assert _captured().run(_ctx(), "capture: refetch") == {"chunks": 3}

    def test_a_failed_fetch_is_an_empty_result(self) -> None:
        with patch(_FETCH_BODY, side_effect=OSError("down")):
            result = _captured().run(_ctx(), "capture: refetch")
        assert result == {"chunks": 0, "sections": 0}

    def test_a_refusal_is_an_empty_result_naming_the_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        refusal = UnsupportedContentError("content rejected: unsupported media type x")
        with caplog.at_level(logging.WARNING), patch(_FETCH_BODY, side_effect=refusal):
            result = _captured("https://u:secret@x.test/p?token=abc").run(
                _ctx(), "capture: refetch"
            )
        assert result["rejected"] == "content rejected: unsupported media type x"
        assert "secret" not in caplog.text
        assert "token" not in caplog.text
