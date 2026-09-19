"""Behaviour of :class:`quarry.daemon_capture.DaemonCaptureSender`.

Every door (``send_capture``, ``send_ingest_url``, ``send_remember``) shares one
``_send`` boundary; the four failure classes it names must each come back as
``False`` with a distinct log line, never propagate, and a healthy post must
carry the short hook timeout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from quarry.api import CaptureIngestRequest, IngestRequest, RememberRequest
from quarry.client import (
    ClientConfigError,
    HttpError,
    QuarryConnectionError,
    QuarryError,
)
from quarry.daemon_capture import _CAPTURE_SEND_TIMEOUT, DaemonCaptureSender

if TYPE_CHECKING:
    from collections.abc import Iterator

_REMEMBER = RememberRequest(name="subagent-abc-report", content="did the thing")
_CAPTURE = CaptureIngestRequest(content="hi", session_id="abc")
_INGEST = IngestRequest(source="https://example.com/x")


@pytest.fixture
def daemon_client() -> Iterator[MagicMock]:
    """A stand-in ``QuarryClient`` returned by ``TargetResolver.connect``."""
    client = MagicMock()
    with patch("quarry.client.TargetResolver.connect", return_value=client):
        yield client


class TestSendRemember:
    def test_success_returns_true_with_hook_timeout(
        self, daemon_client: MagicMock
    ) -> None:
        assert DaemonCaptureSender().send_remember(_REMEMBER, unreachable_log="x")
        daemon_client.remember.assert_called_once_with(
            _REMEMBER, timeout=_CAPTURE_SEND_TIMEOUT
        )

    @pytest.mark.parametrize(
        ("error", "logged"),
        [
            (ClientConfigError("bad QUARRY_URL"), "misconfigured"),
            (QuarryConnectionError("refused", "http://127.0.0.1:8420"), "no daemon"),
            (HttpError("bad request", 400, ""), "rejected request: HTTP 400"),
            (QuarryError("not json"), "malformed response"),
        ],
    )
    def test_each_failure_class_returns_false_and_logs(
        self,
        daemon_client: MagicMock,
        caplog: pytest.LogCaptureFixture,
        error: QuarryError,
        logged: str,
    ) -> None:
        daemon_client.remember.side_effect = error
        with caplog.at_level("WARNING", logger="quarry.daemon_capture"):
            sent = DaemonCaptureSender().send_remember(
                _REMEMBER, unreachable_log="no daemon"
            )
        assert sent is False
        assert any(logged in rec.getMessage() for rec in caplog.records)

    def test_non_quarry_error_propagates(self, daemon_client: MagicMock) -> None:
        """A programmer error is not one of the four classes -- it must surface."""
        daemon_client.remember.side_effect = TypeError("wrong shape")
        with pytest.raises(TypeError, match="wrong shape"):
            DaemonCaptureSender().send_remember(_REMEMBER, unreachable_log="x")


class TestOtherDoorsShareTheBoundary:
    def test_send_capture_uses_hook_timeout(self, daemon_client: MagicMock) -> None:
        assert DaemonCaptureSender().send_capture(_CAPTURE, unreachable_log="x")
        daemon_client.capture.assert_called_once_with(
            _CAPTURE, timeout=_CAPTURE_SEND_TIMEOUT
        )

    def test_send_ingest_url_uses_hook_timeout(self, daemon_client: MagicMock) -> None:
        assert DaemonCaptureSender().send_ingest_url(_INGEST, unreachable_log="x")
        daemon_client.ingest_url.assert_called_once_with(
            _INGEST, timeout=_CAPTURE_SEND_TIMEOUT
        )

    def test_unreachable_log_is_the_callers_phrase(
        self, daemon_client: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        daemon_client.capture.side_effect = QuarryConnectionError("down", "h")
        with caplog.at_level("WARNING", logger="quarry.daemon_capture"):
            DaemonCaptureSender().send_capture(
                _CAPTURE, unreachable_log="pre-compact: run backfill-sessions"
            )
        assert any(
            "pre-compact: run backfill-sessions" in rec.getMessage()
            for rec in caplog.records
        )
