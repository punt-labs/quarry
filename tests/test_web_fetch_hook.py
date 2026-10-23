"""Tests for quarry.web_fetch_hook: the capture hook's decisions, one by one."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from quarry.client import HttpError, QuarryConnectionError
from quarry.send_outcome import SendOutcome
from quarry.web_fetch_hook import WebFetchHook

if TYPE_CHECKING:
    import pytest

_SEND_CAPTURE = "quarry.daemon_capture.DaemonCaptureSender.send_capture"
_SEND_INGEST = "quarry.daemon_capture.DaemonCaptureSender.send_ingest_url"


def _payload(url: str, content: str = "") -> dict[str, object]:
    payload: dict[str, object] = {"tool_input": {"url": url}}
    if content:
        payload["tool_response"] = json.dumps({"result": content})
    return payload


def test_a_payload_without_a_url_is_skipped() -> None:
    with patch(_SEND_CAPTURE) as cap, patch(_SEND_INGEST) as ing:
        assert WebFetchHook({}).run() == {}
    cap.assert_not_called()
    ing.assert_not_called()


def test_a_url_that_could_never_be_stored_is_skipped() -> None:
    with patch(_SEND_CAPTURE) as cap, patch(_SEND_INGEST) as ing:
        assert WebFetchHook(_payload("http://u:S@h.test/p", "<html/>")).run() == {}
    cap.assert_not_called()
    ing.assert_not_called()


def test_fetched_content_is_sent_under_the_storage_name() -> None:
    with patch(_SEND_CAPTURE, return_value=SendOutcome(sent=True)) as cap:
        WebFetchHook(_payload("https://h.test/p?token=S#f", "<html>x</html>")).run()
    request = cap.call_args[0][0]
    assert request.document_name == "https://h.test/p"
    assert request.source_url == "https://h.test/p?token=S#f"


def test_a_daemon_refusal_is_not_traced_as_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = MagicMock()
    client.capture.side_effect = HttpError("content rejected", 400, "")
    with (
        patch("quarry.client.TargetResolver.connect", return_value=client),
        caplog.at_level(logging.INFO),
    ):
        WebFetchHook(_payload("https://h.test/p", "<html>x</html>")).run()
    line = next(r for r in caplog.records if r.name == "quarry.hooks").getMessage()
    assert "-> error:daemon-refused" in line
    assert "daemon-unreachable" not in line


def test_an_unreachable_daemon_is_traced_as_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = MagicMock()
    client.capture.side_effect = QuarryConnectionError("refused", "http://x:1")
    with (
        patch("quarry.client.TargetResolver.connect", return_value=client),
        caplog.at_level(logging.INFO),
    ):
        WebFetchHook(_payload("https://h.test/p", "<html>x</html>")).run()
    line = next(r for r in caplog.records if r.name == "quarry.hooks").getMessage()
    assert "-> error:daemon-unreachable" in line


def test_without_content_the_daemon_is_asked_to_refetch() -> None:
    with (
        patch(_SEND_CAPTURE) as cap,
        patch(_SEND_INGEST, return_value=SendOutcome(sent=True)) as ing,
    ):
        WebFetchHook(_payload("https://h.test/p")).run()
    cap.assert_not_called()
    assert ing.call_args[0][0].source == "https://h.test/p"
