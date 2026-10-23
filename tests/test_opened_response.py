"""Tests for quarry.ingestion.opened_response: the guarded open and bounded read."""

from __future__ import annotations

from http.client import HTTPMessage, HTTPResponse
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from urllib.request import Request

import pytest

from quarry.ingestion.fetch_refused import FetchRefusedError
from quarry.ingestion.opened_response import OpenedResponse

if TYPE_CHECKING:
    from collections.abc import Iterator

_OPEN = "quarry.ingestion.opened_response.GUARDED_OPENER.open"


@pytest.fixture
def response() -> Iterator[MagicMock]:
    resp = MagicMock(spec=HTTPResponse)
    headers = HTTPMessage()
    headers["Content-Type"] = "text/plain; charset=utf-8"
    resp.headers = headers
    resp.url = "https://example.com/p"
    resp.read.side_effect = [b"hello", b""]
    yield resp


def test_the_body_is_read_and_decoded(response: MagicMock) -> None:
    with patch(_OPEN, return_value=response):
        opened = OpenedResponse.open(Request("https://example.com/p"), 5, 1e12)
    assert opened.decode() == "hello"
    assert opened.url == "https://example.com/p"
    assert opened.content_type() == "text/plain; charset=utf-8"


def test_leaving_the_context_closes_the_response(response: MagicMock) -> None:
    with (
        patch(_OPEN, return_value=response),
        OpenedResponse.open(Request("https://example.com/p"), 5, 1e12),
    ):
        pass
    response.close.assert_called_once_with()


def test_an_open_failure_is_a_url_free_refusal_without_a_cause() -> None:
    failure = URLError("refused https://example.com/p?token=SECRET")
    with patch(_OPEN, side_effect=failure), pytest.raises(FetchRefusedError) as info:
        OpenedResponse.open(Request("https://example.com/p"), 5, 1e12)
    assert str(info.value) == "fetch failed: cannot reach the host (str)"
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


def test_a_deadline_already_past_aborts_the_read(response: MagicMock) -> None:
    with patch(_OPEN, return_value=response):
        opened = OpenedResponse.open(Request("https://example.com/p"), 5, 0.0)
    with pytest.raises(FetchRefusedError) as info:
        opened.decode()
    assert str(info.value) == "fetch failed: exceeded its total time budget"


def test_a_value_error_from_the_opener_is_not_a_refusal() -> None:
    bug = ValueError("a bug quoting https://example.com/p?token=SECRET")
    with (
        patch(_OPEN, side_effect=bug),
        pytest.raises(ValueError, match="a bug") as info,
    ):
        OpenedResponse.open(Request("https://example.com/p"), 5, 1e12)
    assert not isinstance(info.value, FetchRefusedError)


def test_a_policy_rejection_from_the_opener_is_still_a_refusal() -> None:
    from quarry.url_safety import UrlRejectedError

    failure = UrlRejectedError("host resolves to a blocked address")
    with patch(_OPEN, side_effect=failure), pytest.raises(FetchRefusedError) as info:
        OpenedResponse.open(Request("https://example.com/p"), 5, 1e12)
    assert str(info.value) == "URL rejected: host resolves to a blocked address"


def test_a_refusal_logs_the_redacted_subject_never_the_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failure = URLError("refused")
    with (
        patch(_OPEN, side_effect=failure),
        caplog.at_level("WARNING"),
        pytest.raises(FetchRefusedError),
    ):
        OpenedResponse.open(
            Request("https://u:pw@example.com/SECRETPATH?t=SECRETQ"), 5, 1e12
        )
    assert "https://example.com/…(segments=1)" in caplog.text
    assert "SECRET" not in caplog.text
    assert "pw@" not in caplog.text
