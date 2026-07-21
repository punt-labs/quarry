"""Tests for quarry.ingestion.web_fetch — HTTP(S) fetch with HTML validation."""

from __future__ import annotations

from http.client import HTTPResponse
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from quarry.ingestion import web_fetch as wf
from quarry.ingestion.web_fetch import WebFetcher


class TestWebFetcher:
    """Test the fetch helper with mocked responses."""

    def test_rejects_non_http(self) -> None:
        with pytest.raises(ValueError, match="Only HTTP"):
            WebFetcher().fetch("ftp://example.com")

    def test_rejects_file_scheme(self) -> None:
        with pytest.raises(ValueError, match="Only HTTP"):
            WebFetcher().fetch("file:///etc/passwd")

    @patch("urllib.request.urlopen")
    def test_fetches_html(self, mock_urlopen: MagicMock) -> None:
        body = b"<html><body><p>Hello</p></body></html>"
        mock_urlopen.return_value = _mock_response(body, "text/html; charset=utf-8")
        assert "Hello" in WebFetcher().fetch("https://example.com")

    @patch("urllib.request.urlopen")
    def test_rejects_non_html_content_type(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_response(b"%PDF-1.4", "application/pdf")
        with pytest.raises(ValueError, match="non-HTML"):
            WebFetcher().fetch("https://example.com/report.pdf")

    @patch("urllib.request.urlopen")
    def test_accepts_xhtml(self, mock_urlopen: MagicMock) -> None:
        body = b"<html><body><p>XHTML</p></body></html>"
        mock_urlopen.return_value = _mock_response(body, "application/xhtml+xml")
        assert "XHTML" in WebFetcher().fetch("https://example.com")

    @patch("urllib.request.urlopen")
    def test_content_type_case_insensitive(self, mock_urlopen: MagicMock) -> None:
        body = b"<html><body><p>OK</p></body></html>"
        mock_urlopen.return_value = _mock_response(body, "Text/HTML; charset=UTF-8")
        assert "OK" in WebFetcher().fetch("https://example.com")

    @patch("urllib.request.urlopen")
    def test_missing_content_type_allowed(self, mock_urlopen: MagicMock) -> None:
        body = b"<html><body><p>No CT</p></body></html>"
        mock_urlopen.return_value = _mock_response(body, "")
        assert "No CT" in WebFetcher().fetch("https://example.com")

    @patch("urllib.request.urlopen")
    def test_rejects_redirect_to_non_http(self, mock_urlopen: MagicMock) -> None:
        mock_resp = _mock_response(b"", "text/html")
        mock_resp.url = "ftp://evil.com/file"
        mock_urlopen.return_value = mock_resp
        with pytest.raises(ValueError, match="Redirect left HTTP"):
            WebFetcher().fetch("https://example.com/redirect")

    @patch("urllib.request.urlopen")
    def test_http_error_raises_valueerror(self, mock_urlopen: MagicMock) -> None:
        from urllib.error import HTTPError

        mock_urlopen.side_effect = HTTPError(
            "https://example.com/missing",
            404,
            "Not Found",
            {},  # type: ignore[arg-type]
            None,
        )
        with pytest.raises(ValueError, match="HTTP 404"):
            WebFetcher().fetch("https://example.com/missing")

    @patch("urllib.request.urlopen")
    def test_url_error_raises_oserror(self, mock_urlopen: MagicMock) -> None:
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Name or service not known")
        with pytest.raises(OSError, match="Cannot reach"):
            WebFetcher().fetch("https://nonexistent.invalid")

    @patch("urllib.request.urlopen")
    def test_custom_timeout_passed_through(self, mock_urlopen: MagicMock) -> None:
        body = b"<html><body><p>Hi</p></body></html>"
        mock_urlopen.return_value = _mock_response(body, "text/html")
        WebFetcher(timeout=7).fetch("https://example.com")
        assert mock_urlopen.call_args.kwargs["timeout"] == 7

    @patch("urllib.request.urlopen")
    def test_oversize_body_fails_at_the_cap(self, mock_urlopen: MagicMock) -> None:
        """A body past the size cap fails cleanly instead of streaming forever."""
        oversize = b"x" * (wf._MAX_RESPONSE_BYTES + 1)
        mock_urlopen.return_value = _mock_response(oversize, "text/html")
        with pytest.raises(ValueError, match=r"exceeds .*-byte cap"):
            WebFetcher().fetch("https://example.com/huge")

    @patch("urllib.request.urlopen")
    def test_slow_drip_aborts_at_the_deadline(
        self, mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A body that never ends is aborted once the wall-clock deadline passes.

        The fake clock stays put while the deadline is computed, then jumps past
        it, so the read loop trips ``TimeoutError`` rather than draining a body
        that keeps producing chunks under every per-socket-op timeout.
        """
        drip = _mock_response(b"", "text/html")
        drip.read.side_effect = None
        drip.read.return_value = b"x" * 16  # always more bytes, never EOF
        clock = iter([0.0, 0.0, 1_000_000.0, 1_000_000.0, 1_000_000.0])
        monkeypatch.setattr(wf, "monotonic", lambda: next(clock))
        mock_urlopen.return_value = drip
        # fetch() wraps the deadline TimeoutError with the URL so concurrent
        # fetches are distinguishable in logs.
        with pytest.raises(TimeoutError, match="time budget") as excinfo:
            WebFetcher().fetch("https://example.com/slow")
        assert "https://example.com/slow" in str(excinfo.value)


def _mock_response(
    body: bytes,
    content_type: str,
    final_url: str = "https://example.com",
) -> MagicMock:
    """Create a mock HTTP response yielding *body* once, then EOF.

    ``read`` returns the whole body on its first chunked call and an empty
    ``bytes`` afterwards, matching the fetcher's read-to-EOF loop.
    """
    mock_resp = MagicMock(spec=HTTPResponse)
    mock_resp.read.side_effect = [body, b""]
    mock_resp.headers = _make_headers(content_type)
    # urllib sets ``.url`` on the response at runtime; it is not a class
    # attribute, so a spec'd mock only exposes it once assigned explicitly.
    mock_resp.url = final_url
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_headers(content_type: str) -> Any:
    """Create a mock headers object with Content-Type."""
    from email.message import Message

    msg = Message()
    if content_type:
        msg["Content-Type"] = content_type
    return msg
