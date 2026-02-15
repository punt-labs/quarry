"""Tests for URL ingestion: fetch, process HTML, chunk, embed, store."""

from __future__ import annotations

from http.client import HTTPResponse
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from quarry.html_processor import process_html_text
from quarry.models import PageType


class TestProcessHtmlText:
    """Test the shared HTML-to-pages function used by both file and URL paths."""

    def test_basic_html(self):
        html = "<html><body><h1>Title</h1><p>Content here.</p></body></html>"
        pages = process_html_text(html, "test.html", "https://example.com")
        assert len(pages) >= 1
        assert pages[0].document_name == "test.html"
        assert pages[0].document_path == "https://example.com"
        assert pages[0].page_type == PageType.SECTION

    def test_strips_boilerplate(self):
        html = (
            "<html><body>"
            "<nav>Menu</nav>"
            "<script>alert(1)</script>"
            "<main><h1>Article</h1><p>Real content.</p></main>"
            "<footer>Copyright</footer>"
            "</body></html>"
        )
        pages = process_html_text(html, "doc", "https://example.com")
        text = " ".join(p.text for p in pages)
        assert "Real content" in text
        assert "Menu" not in text
        assert "alert" not in text
        assert "Copyright" not in text

    def test_empty_html_returns_empty(self):
        pages = process_html_text("<html><body></body></html>", "e", "u")
        assert pages == []

    def test_title_prepended_when_no_headings(self):
        html = (
            "<html><head><title>My Page</title></head>"
            "<body><p>Some text.</p></body></html>"
        )
        pages = process_html_text(html, "doc", "u")
        assert any("My Page" in p.text for p in pages)


class TestFetchUrl:
    """Test the HTTP fetch helper with mocked responses."""

    def test_rejects_non_http(self):
        from quarry.pipeline import _fetch_url

        with pytest.raises(ValueError, match="Only HTTP"):
            _fetch_url("ftp://example.com")

    def test_rejects_file_scheme(self):
        from quarry.pipeline import _fetch_url

        with pytest.raises(ValueError, match="Only HTTP"):
            _fetch_url("file:///etc/passwd")

    @patch("urllib.request.urlopen")
    def test_fetches_html(self, mock_urlopen: MagicMock):
        from quarry.pipeline import _fetch_url

        body = b"<html><body><p>Hello</p></body></html>"
        mock_resp = _mock_response(body, "text/html; charset=utf-8")
        mock_urlopen.return_value = mock_resp

        result = _fetch_url("https://example.com")
        assert "Hello" in result

    @patch("urllib.request.urlopen")
    def test_rejects_non_html_content_type(self, mock_urlopen: MagicMock):
        from quarry.pipeline import _fetch_url

        mock_resp = _mock_response(b"%PDF-1.4", "application/pdf")
        mock_urlopen.return_value = mock_resp

        with pytest.raises(ValueError, match="non-HTML"):
            _fetch_url("https://example.com/report.pdf")

    @patch("urllib.request.urlopen")
    def test_accepts_xhtml(self, mock_urlopen: MagicMock):
        from quarry.pipeline import _fetch_url

        body = b"<html><body><p>XHTML</p></body></html>"
        mock_resp = _mock_response(body, "application/xhtml+xml")
        mock_urlopen.return_value = mock_resp

        result = _fetch_url("https://example.com")
        assert "XHTML" in result

    @patch("urllib.request.urlopen")
    def test_content_type_case_insensitive(self, mock_urlopen: MagicMock):
        from quarry.pipeline import _fetch_url

        body = b"<html><body><p>OK</p></body></html>"
        mock_resp = _mock_response(body, "Text/HTML; charset=UTF-8")
        mock_urlopen.return_value = mock_resp

        result = _fetch_url("https://example.com")
        assert "OK" in result

    @patch("urllib.request.urlopen")
    def test_missing_content_type_allowed(self, mock_urlopen: MagicMock):
        from quarry.pipeline import _fetch_url

        body = b"<html><body><p>No CT</p></body></html>"
        mock_resp = _mock_response(body, "")
        mock_urlopen.return_value = mock_resp

        result = _fetch_url("https://example.com")
        assert "No CT" in result

    @patch("urllib.request.urlopen")
    def test_rejects_redirect_to_non_http(self, mock_urlopen: MagicMock):
        from quarry.pipeline import _fetch_url

        mock_resp = _mock_response(b"", "text/html")
        mock_resp.geturl.return_value = "ftp://evil.com/file"
        mock_urlopen.return_value = mock_resp

        with pytest.raises(ValueError, match="Redirect left HTTP"):
            _fetch_url("https://example.com/redirect")

    @patch("urllib.request.urlopen")
    def test_http_error_raises_valueerror(self, mock_urlopen: MagicMock):
        from urllib.error import HTTPError

        from quarry.pipeline import _fetch_url

        mock_urlopen.side_effect = HTTPError(
            "https://example.com/missing",
            404,
            "Not Found",
            {},  # type: ignore[arg-type]
            None,
        )
        with pytest.raises(ValueError, match="HTTP 404"):
            _fetch_url("https://example.com/missing")

    @patch("urllib.request.urlopen")
    def test_url_error_raises_oserror(self, mock_urlopen: MagicMock):
        from urllib.error import URLError

        from quarry.pipeline import _fetch_url

        mock_urlopen.side_effect = URLError("Name or service not known")
        with pytest.raises(OSError, match="Cannot reach"):
            _fetch_url("https://nonexistent.invalid")


class TestIngestUrl:
    """Integration test: fetch -> process -> chunk -> embed -> store."""

    @patch("quarry.pipeline._fetch_url")
    def test_end_to_end(self, mock_fetch: MagicMock):
        from quarry.pipeline import ingest_url

        mock_fetch.return_value = (
            "<html><head><title>Docs</title></head>"
            "<body><h1>API Reference</h1>"
            "<p>The authenticate endpoint accepts a JWT token.</p>"
            "</body></html>"
        )

        settings = MagicMock()
        settings.chunk_max_chars = 1800
        settings.chunk_overlap_chars = 200

        db = MagicMock()
        db.open_table.return_value = MagicMock()

        with patch("quarry.pipeline.get_embedding_backend") as mock_embed_factory:
            mock_backend = MagicMock()
            mock_backend.model_name = "test-model"
            mock_backend.embed_texts.return_value = np.zeros(
                (10, 768), dtype=np.float32
            )
            mock_embed_factory.return_value = mock_backend

            with patch("quarry.pipeline.insert_chunks", return_value=1):
                result = ingest_url(
                    "https://docs.example.com/api",
                    db,
                    settings,
                    collection="docs",
                )

        assert result["document_name"] == "https://docs.example.com/api"
        assert result["collection"] == "docs"
        assert result["chunks"] >= 1
        mock_fetch.assert_called_once_with("https://docs.example.com/api", timeout=30)

    @patch("quarry.pipeline._fetch_url")
    def test_custom_document_name(self, mock_fetch: MagicMock):
        from quarry.pipeline import ingest_url

        mock_fetch.return_value = "<html><body><p>Content.</p></body></html>"

        settings = MagicMock()
        settings.chunk_max_chars = 1800
        settings.chunk_overlap_chars = 200

        db = MagicMock()
        db.open_table.return_value = MagicMock()

        with (
            patch("quarry.pipeline.get_embedding_backend") as mock_embed_factory,
            patch("quarry.pipeline.insert_chunks", return_value=1),
        ):
            mock_backend = MagicMock()
            mock_backend.model_name = "test-model"
            mock_backend.embed_texts.return_value = np.zeros(
                (10, 768), dtype=np.float32
            )
            mock_embed_factory.return_value = mock_backend

            result = ingest_url(
                "https://example.com/page",
                db,
                settings,
                document_name="my-page",
            )

        assert result["document_name"] == "my-page"


def _mock_response(
    body: bytes,
    content_type: str,
    final_url: str = "https://example.com",
) -> MagicMock:
    """Create a mock HTTP response with headers and context manager."""
    mock_resp = MagicMock(spec=HTTPResponse)
    mock_resp.read.return_value = body
    mock_resp.headers = _make_headers(content_type)
    mock_resp.geturl.return_value = final_url
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
