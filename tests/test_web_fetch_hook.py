"""Tests for quarry.web_fetch_hook: the capture hook's decisions, one by one."""

from __future__ import annotations

import json
from unittest.mock import patch

from quarry.web_fetch_hook import WebFetchHook

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
    with patch(_SEND_CAPTURE, return_value=True) as cap:
        WebFetchHook(_payload("https://h.test/p?token=S#f", "<html>x</html>")).run()
    request = cap.call_args[0][0]
    assert request.document_name == "https://h.test/p"
    assert request.source_url == "https://h.test/p?token=S#f"


def test_without_content_the_daemon_is_asked_to_refetch() -> None:
    with patch(_SEND_CAPTURE) as cap, patch(_SEND_INGEST, return_value=True) as ing:
        WebFetchHook(_payload("https://h.test/p")).run()
    cap.assert_not_called()
    assert ing.call_args[0][0].source == "https://h.test/p"
