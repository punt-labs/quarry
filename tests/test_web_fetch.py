"""Tests for quarry.ingestion.web_fetch — HTTP(S) fetch with HTML validation."""

from __future__ import annotations

from http.client import HTTPResponse
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

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


def _mock_response(
    body: bytes,
    content_type: str,
    final_url: str = "https://example.com",
) -> MagicMock:
    """Create a mock HTTP response with headers and context manager."""
    mock_resp = MagicMock(spec=HTTPResponse)
    mock_resp.read.return_value = body
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
