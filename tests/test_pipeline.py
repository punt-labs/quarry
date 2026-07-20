from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from quarry.config import Settings
from quarry.db import Database
from quarry.models import Chunk, PageAnalysis, PageContent, PageType


def _settings() -> Settings:
    return Settings()


def _make_page_content(
    page_number: int, page_type: PageType, text: str = "some text"
) -> PageContent:
    return PageContent(
        document_name="test.pdf",
        document_path="/tmp/test.pdf",
        page_number=page_number,
        total_pages=5,
        text=text,
        page_type=page_type,
    )


def _mock_ocr_backend(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ocr_document_return: list[PageContent] | None = None,
) -> MagicMock:
    """Mock get_ocr_backend to return a backend with configurable returns."""
    backend = MagicMock()
    if ocr_document_return is not None:
        backend.ocr_document.return_value = ocr_document_return
    monkeypatch.setattr(
        "quarry.ingestion.pipeline.get_ocr_backend", lambda _settings: backend
    )
    return backend


def _mock_embedding_backend(
    monkeypatch: pytest.MonkeyPatch,
    vectors: np.ndarray,
) -> MagicMock:
    """Mock get_embedding_backend to return a backend with given vectors."""
    backend = MagicMock()
    backend.embed_texts.return_value = vectors
    backend.model_name = "test-model"
    monkeypatch.setattr(
        "quarry.ingestion.streaming.get_embedding_backend", lambda _settings: backend
    )
    return backend


class TestIngestDocument:
    def test_file_not_found(self):
        from quarry.ingestion.pipeline import ingest_document

        db = Database(MagicMock())
        with pytest.raises(FileNotFoundError):
            ingest_document(
                Path("/nonexistent/file.pdf"),
                db,
                _settings(),
            )

    def test_all_text_pages(self, monkeypatch, tmp_path: Path):
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        analyses = [
            PageAnalysis(page_number=1, page_type=PageType.TEXT, text_length=100),
            PageAnalysis(page_number=2, page_type=PageType.TEXT, text_length=200),
        ]
        text_pages = [
            _make_page_content(1, PageType.TEXT),
            _make_page_content(2, PageType.TEXT),
        ]
        chunks = [
            Chunk(
                document_name="test.pdf",
                document_path=str(pdf_file),
                collection="default",
                page_number=1,
                total_pages=2,
                chunk_index=0,
                text="chunk",
                page_raw_text="full",
                page_type="text",
                source_format=".pdf",
                ingestion_timestamp=datetime.now(tz=UTC),
            )
        ]
        vectors = np.zeros((1, 768), dtype=np.float32)

        monkeypatch.setattr(
            "quarry.extractors.pdf_extractor.PdfExtractor._classify_pages",
            staticmethod(lambda _path: analyses),
        )
        monkeypatch.setattr(
            "quarry.extractors.pdf_extractor.extract_text_pages",
            lambda _path, _pages, _total, **_kw: text_pages,
        )
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: chunks,
        )
        _mock_embedding_backend(monkeypatch, vectors)
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert",
            lambda _db, _chunks, _vectors: 1,
        )

        from quarry.ingestion.pipeline import ingest_document

        db = Database(MagicMock())
        result = ingest_document(pdf_file, db, _settings())

        assert result["document_name"] == "test.pdf"
        total_pages = result.get("total_pages")
        assert total_pages is not None
        assert total_pages == 2
        text_page_count = result.get("text_pages")
        assert text_page_count is not None
        assert text_page_count == 2
        image_page_count = result.get("image_pages")
        assert image_page_count is not None
        assert image_page_count == 0
        assert result["chunks"] == 1

    def test_all_image_pages(self, monkeypatch, tmp_path: Path):
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        analyses = [
            PageAnalysis(page_number=1, page_type=PageType.IMAGE, text_length=0),
        ]
        ocr_pages = [_make_page_content(1, PageType.IMAGE, "ocr text")]
        chunks = [
            Chunk(
                document_name="test.pdf",
                document_path=str(pdf_file),
                collection="default",
                page_number=1,
                total_pages=1,
                chunk_index=0,
                text="ocr text",
                page_raw_text="ocr text",
                page_type="text",
                source_format=".pdf",
                ingestion_timestamp=datetime.now(tz=UTC),
            )
        ]
        vectors = np.zeros((1, 768), dtype=np.float32)

        monkeypatch.setattr(
            "quarry.extractors.pdf_extractor.PdfExtractor._classify_pages",
            staticmethod(lambda _path: analyses),
        )
        _mock_ocr_backend(monkeypatch, ocr_document_return=ocr_pages)
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: chunks,
        )
        _mock_embedding_backend(monkeypatch, vectors)
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert",
            lambda _db, _chunks, _vectors: 1,
        )

        from quarry.ingestion.pipeline import ingest_document

        db = Database(MagicMock())
        result = ingest_document(pdf_file, db, _settings())

        image_pages = result.get("image_pages")
        assert image_pages is not None
        assert image_pages == 1
        text_pages = result.get("text_pages")
        assert text_pages is not None
        assert text_pages == 0

    def test_no_text_extracted(self, monkeypatch, tmp_path: Path):
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        analyses = [
            PageAnalysis(page_number=1, page_type=PageType.IMAGE, text_length=0),
        ]
        ocr_pages = [_make_page_content(1, PageType.IMAGE, "")]

        monkeypatch.setattr(
            "quarry.extractors.pdf_extractor.PdfExtractor._classify_pages",
            staticmethod(lambda _path: analyses),
        )
        _mock_ocr_backend(monkeypatch, ocr_document_return=ocr_pages)
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )

        from quarry.ingestion.pipeline import ingest_document

        db = Database(MagicMock())
        result = ingest_document(pdf_file, db, _settings())

        assert result["chunks"] == 0

    def test_overwrite_deletes_existing(self, monkeypatch, tmp_path: Path):
        """Overwrite deletes the prior document — but only once chunks exist.

        The delete is gated on a replacement chunk set (see
        ``test_overwrite_keeps_prior_when_chunks_empty`` for the zero-chunk case),
        so this ingest produces one chunk and asserts the prior copy is deleted.
        """
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        analyses = [
            PageAnalysis(page_number=1, page_type=PageType.TEXT, text_length=100),
        ]
        text_pages = [_make_page_content(1, PageType.TEXT)]
        chunks = [
            Chunk(
                document_name="test.pdf",
                document_path=str(pdf_file),
                collection="default",
                page_number=1,
                total_pages=1,
                chunk_index=0,
                text="chunk",
                page_raw_text="full",
                page_type="text",
                source_format=".pdf",
                ingestion_timestamp=datetime.now(tz=UTC),
            )
        ]

        monkeypatch.setattr(
            "quarry.extractors.pdf_extractor.PdfExtractor._classify_pages",
            staticmethod(lambda _path: analyses),
        )
        monkeypatch.setattr(
            "quarry.extractors.pdf_extractor.extract_text_pages",
            lambda _path, _pages, _total, **_kw: text_pages,
        )
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: chunks,
        )
        _mock_embedding_backend(monkeypatch, np.zeros((1, 768), dtype=np.float32))
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert",
            lambda _db, _chunks, _vectors: 1,
        )

        delete_called_with: list[str] = []

        def _mock_delete(_db: object, name: str, **_kw: object) -> int:
            delete_called_with.append(name)
            return 0

        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.delete_document", _mock_delete
        )

        from quarry.ingestion.pipeline import ingest_document

        db = Database(MagicMock())
        ingest_document(pdf_file, db, _settings(), overwrite=True)

        assert delete_called_with == ["test.pdf"]

    def test_overwrite_keeps_prior_when_chunks_empty(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Overwrite must NOT delete when extraction chunks to zero.

        Pages that parse but chunk to nothing must not delete the prior good
        document and then store nothing — the fail-closed overwrite gate fires
        only when a replacement chunk set exists.
        """
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        analyses = [
            PageAnalysis(page_number=1, page_type=PageType.TEXT, text_length=100),
        ]
        text_pages = [_make_page_content(1, PageType.TEXT)]
        monkeypatch.setattr(
            "quarry.extractors.pdf_extractor.PdfExtractor._classify_pages",
            staticmethod(lambda _path: analyses),
        )
        monkeypatch.setattr(
            "quarry.extractors.pdf_extractor.extract_text_pages",
            lambda _path, _pages, _total, **_kw: text_pages,
        )
        # Pages extract, but chunking yields nothing.
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )

        deleted: list[str] = []
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.delete_document",
            lambda _db, name, **_kw: deleted.append(name),
        )

        from quarry.ingestion.pipeline import ingest_document

        db = Database(MagicMock())
        result = ingest_document(pdf_file, db, _settings(), overwrite=True)

        assert deleted == []  # prior document preserved on empty chunk set
        assert result["chunks"] == 0

    def test_progress_callback(self, monkeypatch, tmp_path: Path):
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        monkeypatch.setattr(
            "quarry.extractors.pdf_extractor.PdfExtractor._classify_pages",
            staticmethod(
                lambda _path: [
                    PageAnalysis(
                        page_number=1, page_type=PageType.TEXT, text_length=100
                    )
                ]
            ),
        )
        monkeypatch.setattr(
            "quarry.extractors.pdf_extractor.extract_text_pages",
            lambda _path, _pages, _total, **_kw: [_make_page_content(1, PageType.TEXT)],
        )
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )

        from quarry.ingestion.pipeline import ingest_document

        messages: list[str] = []
        db = Database(MagicMock())
        ingest_document(pdf_file, db, _settings(), progress_callback=messages.append)

        assert len(messages) > 0
        assert any("Analyzing" in m for m in messages)

    def test_dispatches_text_file(self, monkeypatch, tmp_path: Path):
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("Hello.\n\nWorld.")

        chunks = [
            Chunk(
                document_name="notes.txt",
                document_path=str(txt_file),
                collection="default",
                page_number=1,
                total_pages=2,
                chunk_index=0,
                text="Hello.",
                page_raw_text="Hello.",
                page_type="text",
                source_format=".txt",
                ingestion_timestamp=datetime.now(tz=UTC),
            )
        ]
        vectors = np.zeros((1, 768), dtype=np.float32)

        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: chunks,
        )
        _mock_embedding_backend(monkeypatch, vectors)
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert",
            lambda _db, _chunks, _vectors: 1,
        )

        from quarry.ingestion.pipeline import ingest_document

        db = Database(MagicMock())
        result = ingest_document(txt_file, db, _settings())

        assert result["document_name"] == "notes.txt"
        assert result["chunks"] == 1
        sections = result.get("sections")
        assert sections is not None
        assert sections == 2

    def test_dispatches_spreadsheet_file(self, monkeypatch, tmp_path: Path):
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("A,B\n1,2\n")

        chunks = [
            Chunk(
                document_name="data.csv",
                document_path=str(csv_file),
                collection="default",
                page_number=1,
                total_pages=1,
                chunk_index=0,
                text="1 & 2",
                page_raw_text="1 & 2",
                page_type="spreadsheet",
                source_format=".csv",
                ingestion_timestamp=datetime.now(tz=UTC),
            )
        ]
        vectors = np.zeros((1, 768), dtype=np.float32)

        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: chunks,
        )
        _mock_embedding_backend(monkeypatch, vectors)
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert",
            lambda _db, _chunks, _vectors: 1,
        )

        from quarry.ingestion.pipeline import ingest_document

        db = Database(MagicMock())
        result = ingest_document(csv_file, db, _settings())

        assert result["document_name"] == "data.csv"
        assert result["chunks"] == 1
        sections = result.get("sections")
        assert sections is not None
        assert sections == 1

    def test_dispatches_html_file(self, monkeypatch, tmp_path: Path):
        html_file = tmp_path / "article.html"
        html_file.write_text("<html><body><h1>Title</h1><p>Content.</p></body></html>")

        chunks = [
            Chunk(
                document_name="article.html",
                document_path=str(html_file),
                collection="default",
                page_number=1,
                total_pages=1,
                chunk_index=0,
                text="Title Content.",
                page_raw_text="Title Content.",
                page_type="text",
                source_format=".html",
                ingestion_timestamp=datetime.now(tz=UTC),
            )
        ]
        vectors = np.zeros((1, 768), dtype=np.float32)

        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: chunks,
        )
        _mock_embedding_backend(monkeypatch, vectors)
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert",
            lambda _db, _chunks, _vectors: 1,
        )

        from quarry.ingestion.pipeline import ingest_document

        db = Database(MagicMock())
        result = ingest_document(html_file, db, _settings())

        assert result["document_name"] == "article.html"
        assert result["chunks"] == 1

    def test_unsupported_format_raises(self, tmp_path: Path):
        zip_file = tmp_path / "archive.zip"
        zip_file.write_bytes(b"PK\x03\x04")

        from quarry.ingestion.pipeline import ingest_document

        db = Database(MagicMock())
        with pytest.raises(ValueError, match="Unsupported file format"):
            ingest_document(zip_file, db, _settings())


class TestIngestText:
    def test_ingests_raw_text(self, monkeypatch):
        chunks = [
            Chunk(
                document_name="clip.txt",
                document_path="",
                collection="default",
                page_number=1,
                total_pages=1,
                chunk_index=0,
                text="Hello world",
                page_raw_text="Hello world",
                page_type="text",
                source_format="inline",
                ingestion_timestamp=datetime.now(tz=UTC),
            )
        ]
        vectors = np.zeros((1, 768), dtype=np.float32)

        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: chunks,
        )
        _mock_embedding_backend(monkeypatch, vectors)
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert",
            lambda _db, _chunks, _vectors: 1,
        )

        from quarry.ingestion.pipeline import ingest_content

        db = Database(MagicMock())
        result = ingest_content("Hello world", "clip.txt", db, _settings())

        assert result["document_name"] == "clip.txt"
        assert result["chunks"] == 1
        sections = result.get("sections")
        assert sections is not None
        assert sections == 1

    def test_overwrite_deletes_existing(self, monkeypatch):
        """Overwrite deletes the prior document once a replacement chunk exists."""
        chunks = [
            Chunk(
                document_name="doc.txt",
                document_path="",
                collection="default",
                page_number=1,
                total_pages=1,
                chunk_index=0,
                text="text",
                page_raw_text="text",
                page_type="text",
                source_format="inline",
                ingestion_timestamp=datetime.now(tz=UTC),
            )
        ]
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: chunks,
        )
        _mock_embedding_backend(monkeypatch, np.zeros((1, 768), dtype=np.float32))
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert",
            lambda _db, _chunks, _vectors: 1,
        )

        delete_called: list[str] = []

        def _mock_delete(_db: object, name: str, **_kw: object) -> int:
            delete_called.append(name)
            return 0

        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.delete_document", _mock_delete
        )

        from quarry.ingestion.pipeline import ingest_content

        db = Database(MagicMock())
        ingest_content("text", "doc.txt", db, _settings(), overwrite=True)

        assert delete_called == ["doc.txt"]

    def test_overwrite_keeps_prior_when_chunks_empty(self, monkeypatch) -> None:
        """Inline content that chunks to zero must not delete the prior document."""
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )

        deleted: list[str] = []
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.delete_document",
            lambda _db, name, **_kw: deleted.append(name),
        )

        from quarry.ingestion.pipeline import ingest_content

        db = Database(MagicMock())
        result = ingest_content("text", "doc.txt", db, _settings(), overwrite=True)

        assert deleted == []  # prior document preserved on empty chunk set
        assert result["chunks"] == 0

    def test_empty_text_returns_zero_chunks(self, monkeypatch):
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )

        from quarry.ingestion.pipeline import ingest_content

        db = Database(MagicMock())
        result = ingest_content("", "empty.txt", db, _settings())

        assert result["chunks"] == 0


class TestIngestUrlScrubbing:
    """``ingest_url`` scrubs only when a ``content_scrubber`` is supplied.

    The WebFetch auto-capture ingress passes one so its pushable collection is
    PII-clean on both branches; user-initiated ``quarry ingest <url>`` passes
    none, so a deliberately ingested document is stored byte-for-byte.
    """

    _RAW = "contact jmf@pobox.com now"

    def _patch_fetch_and_extract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pages = [PageContent("u", "u", 1, 1, self._RAW, PageType.TEXT)]
        monkeypatch.setattr(
            "quarry.ingestion.web_fetch.WebFetcher.fetch",
            lambda _self, _url: "<html/>",
        )
        monkeypatch.setattr(
            "quarry.extractors.html_extractor.HtmlExtractor.extract_from_html",
            lambda _self, _html, _name, _url: pages,
        )

    def _capture_stored_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> list[PageContent]:
        from quarry.ingestion import pipeline

        captured: list[PageContent] = []

        def _capture(
            pages_arg: list[PageContent], *_a: object, **_k: object
        ) -> dict[str, object]:
            captured.extend(pages_arg)
            return {"document_name": "u", "collection": "web-captures", "chunks": 0}

        monkeypatch.setattr(pipeline, "_chunk_embed_store", _capture)
        return captured

    def test_scrubber_redacts_page_text_before_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from quarry.ingestion.pipeline import ingest_url
        from quarry.scrub import scrub_and_log

        self._patch_fetch_and_extract(monkeypatch)
        captured = self._capture_stored_pages(monkeypatch)

        ingest_url(
            "https://example.com/p",
            Database(MagicMock()),
            _settings(),
            collection="web-captures",
            content_scrubber=lambda t: scrub_and_log(t, "web-fetch"),
        )

        assert captured
        assert "jmf@pobox.com" not in captured[0].text
        assert "[REDACTED:email]" in captured[0].text

    def test_scrubber_redacts_document_name_and_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The URL choke point redacts the metadata the chunker copies on chunks.

        Same guarantee as ``ingest_content``: an explicit document_name and the
        summary are redacted when a scrubber is present, so the capture callers
        forward them raw and the pipeline redacts once.
        """
        from quarry.ingestion import pipeline
        from quarry.ingestion.pipeline import ingest_url
        from quarry.scrub import scrub_and_log

        self._patch_fetch_and_extract(monkeypatch)
        seen: dict[str, object] = {}

        def _capture(
            _pages: object, document_name: str, *_a: object, **kw: object
        ) -> dict[str, object]:
            seen["name"] = document_name
            seen["summary"] = kw["summary"]
            return {"document_name": document_name, "collection": "c", "chunks": 0}

        monkeypatch.setattr(pipeline, "_chunk_embed_store", _capture)
        ingest_url(
            "https://example.com/p",
            Database(MagicMock()),
            _settings(),
            collection="c",
            document_name="note jmf@pobox.com",
            content_scrubber=lambda t: scrub_and_log(t, "web-fetch"),
            summary="contact jmf@pobox.com",
        )

        assert "jmf@pobox.com" not in str(seen["name"])
        assert "[REDACTED:email]" in str(seen["name"])
        assert "jmf@pobox.com" not in str(seen["summary"])
        assert "[REDACTED:email]" in str(seen["summary"])

    def test_empty_extraction_keeps_prior_and_stores_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A re-fetch whose HTML extracts to zero pages must not delete the prior."""
        from quarry.ingestion.pipeline import ingest_url

        monkeypatch.setattr(
            "quarry.ingestion.web_fetch.WebFetcher.fetch",
            lambda _self, _url: "<html></html>",
        )
        monkeypatch.setattr(
            "quarry.extractors.html_extractor.HtmlExtractor.extract_from_html",
            lambda _self, _html, _name, _url: [],
        )
        deleted: list[str] = []
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.delete_document",
            lambda _self, name, **_k: deleted.append(name),
        )
        captured = self._capture_stored_pages(monkeypatch)

        result = ingest_url(
            "https://example.com/p",
            Database(MagicMock()),
            _settings(),
            overwrite=True,
            collection="c",
        )

        assert deleted == []  # prior document not deleted on empty extraction
        assert captured == []  # nothing stored
        assert result["chunks"] == 0

    def test_scrub_raise_keeps_prior_and_stores_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scrub that raises aborts before the overwrite-delete on ingest_url."""
        from quarry.ingestion.pipeline import ingest_url

        self._patch_fetch_and_extract(monkeypatch)
        deleted: list[str] = []
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.delete_document",
            lambda _self, name, **_k: deleted.append(name),
        )
        stored = self._capture_stored_pages(monkeypatch)

        def _boom(_text: str) -> str:
            raise ValueError("scrub failed")

        with pytest.raises(ValueError, match="scrub failed"):
            ingest_url(
                "https://example.com/p",
                Database(MagicMock()),
                _settings(),
                overwrite=True,
                collection="c",
                content_scrubber=_boom,
            )

        assert deleted == []  # prior document preserved
        assert stored == []  # never reached the store

    def test_no_scrubber_leaves_text_byte_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from quarry.ingestion.pipeline import ingest_url

        self._patch_fetch_and_extract(monkeypatch)
        captured = self._capture_stored_pages(monkeypatch)

        ingest_url(
            "https://example.com/p",
            Database(MagicMock()),
            _settings(),
            collection="default",
        )

        assert captured
        assert captured[0].text == self._RAW


class TestIngestContentScrubbing:
    """``ingest_content`` scrubs inline text only when a scrubber is supplied.

    The ``remember`` and ``capture`` front doors both pass one so the database
    copy is PII-clean; a user-initiated ingest passes none and stores the text
    byte-for-byte.  ``format_hint="html"`` routes raw markup through the HTML
    extractor rather than storing tags verbatim.
    """

    _RAW = "reach me at jmf@pobox.com"

    def _capture_stored_pages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> list[PageContent]:
        from quarry.ingestion import pipeline

        captured: list[PageContent] = []

        def _capture(
            pages_arg: list[PageContent], *_a: object, **_k: object
        ) -> dict[str, object]:
            captured.extend(pages_arg)
            return {"document_name": "note", "collection": "memory-x", "chunks": 0}

        monkeypatch.setattr(pipeline, "_chunk_embed_store", _capture)
        return captured

    def test_scrubber_redacts_inline_text_before_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from quarry.ingestion.pipeline import ingest_content
        from quarry.scrub import scrub_and_log

        captured = self._capture_stored_pages(monkeypatch)
        ingest_content(
            self._RAW,
            "note",
            Database(MagicMock()),
            _settings(),
            content_scrubber=lambda t: scrub_and_log(t, "remember"),
        )

        assert captured
        assert "jmf@pobox.com" not in captured[0].text
        assert "[REDACTED:email]" in captured[0].text

    def _capture_stored_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> dict[str, object]:
        from quarry.ingestion import pipeline

        seen: dict[str, object] = {}

        def _capture(
            _pages: object, document_name: str, *_a: object, **kw: object
        ) -> dict[str, object]:
            seen["name"] = document_name
            seen["summary"] = kw["summary"]
            return {"document_name": document_name, "collection": "c", "chunks": 0}

        monkeypatch.setattr(pipeline, "_chunk_embed_store", _capture)
        return seen

    def test_scrubber_redacts_document_name_and_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The choke point redacts the metadata the chunker copies onto chunks.

        A ``content_scrubber`` is the single signal that this is a scrubbed
        ingest, so ``ingest_content`` redacts the document name and summary
        itself — every caller (daemon, stdio MCP, backfill) inherits it and none
        can forget it.
        """
        from quarry.ingestion.pipeline import ingest_content
        from quarry.scrub import scrub_and_log

        seen = self._capture_stored_metadata(monkeypatch)
        ingest_content(
            "body",
            "note jmf@pobox.com",
            Database(MagicMock()),
            _settings(),
            content_scrubber=lambda t: scrub_and_log(t, "remember"),
            summary="contact jmf@pobox.com",
        )

        assert "jmf@pobox.com" not in str(seen["name"])
        assert "[REDACTED:email]" in str(seen["name"])
        assert "jmf@pobox.com" not in str(seen["summary"])
        assert "[REDACTED:email]" in str(seen["summary"])

    def test_no_scrubber_keeps_document_name_and_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plain ingest (no scrubber) stores metadata byte-for-byte."""
        from quarry.ingestion.pipeline import ingest_content

        seen = self._capture_stored_metadata(monkeypatch)
        ingest_content(
            "body",
            "note jmf@pobox.com",
            Database(MagicMock()),
            _settings(),
            summary="contact jmf@pobox.com",
        )

        assert seen["name"] == "note jmf@pobox.com"
        assert seen["summary"] == "contact jmf@pobox.com"

    def test_failed_scrub_writes_zero_chunks_and_keeps_prior(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scrub that raises aborts before store AND before the overwrite-delete.

        The security property (nothing half-redacted is stored) and the
        availability property (the last good document is not traded for nothing)
        are locked together: neither ``_chunk_embed_store`` nor the overwrite
        delete may run when the scrubber raises.
        """
        from quarry.ingestion import pipeline
        from quarry.ingestion.pipeline import ingest_content

        stored: list[int] = []
        deleted: list[str] = []
        monkeypatch.setattr(
            pipeline, "_chunk_embed_store", lambda *_a, **_k: stored.append(1)
        )
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.delete_document",
            lambda _self, name, **_k: deleted.append(name),
        )

        def _boom(_text: str) -> str:
            raise ValueError("scrub failed")

        with pytest.raises(ValueError, match="scrub failed"):
            ingest_content(
                self._RAW,
                "note",
                Database(MagicMock()),
                _settings(),
                overwrite=True,
                content_scrubber=_boom,
            )

        assert stored == []  # never reached the store — zero chunks written
        assert deleted == []  # prior scrubbed copy preserved (fail-closed delete)

    def test_empty_extraction_keeps_prior_and_stores_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An extraction yielding zero pages must not delete the prior document.

        A web-fetch re-capture (overwrite=True) whose HTML extracts to nothing
        (already-markdown, JS-only, non-HTML) must keep the prior good capture
        and report zero chunks — never delete it and falsely report a fresh one.
        """
        from quarry.ingestion import pipeline
        from quarry.ingestion.pipeline import ingest_content

        deleted: list[str] = []
        seen_pages: list[int] = []

        def _store(
            pages_arg: list[PageContent], *_a: object, **_k: object
        ) -> dict[str, object]:
            seen_pages.append(len(pages_arg))
            return {
                "document_name": "note",
                "collection": "c",
                "chunks": len(pages_arg),
            }

        monkeypatch.setattr(pipeline, "_chunk_embed_store", _store)
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.delete_document",
            lambda _self, name, **_k: deleted.append(name),
        )

        result = ingest_content(
            "<html><body></body></html>",
            "note",
            Database(MagicMock()),
            _settings(),
            overwrite=True,
            format_hint="html",
        )

        assert seen_pages == [0]  # the extraction really was empty
        assert deleted == []  # prior document NOT deleted on an empty extraction
        assert result["chunks"] == 0  # honest zero, not a bogus fresh capture

    def test_no_scrubber_leaves_inline_text_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from quarry.ingestion.pipeline import ingest_content

        captured = self._capture_stored_pages(monkeypatch)
        ingest_content(self._RAW, "note", Database(MagicMock()), _settings())

        assert captured
        assert captured[0].text == self._RAW

    def test_html_hint_extracts_markdown_not_raw_tags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from quarry.ingestion.pipeline import ingest_content

        captured = self._capture_stored_pages(monkeypatch)
        ingest_content(
            "<html><body><h1>Title</h1><p>Body text.</p></body></html>",
            "page",
            Database(MagicMock()),
            _settings(),
            format_hint="html",
        )

        assert captured
        stored = "\n".join(p.text for p in captured)
        assert "Body text." in stored
        assert "<h1>" not in stored


class _FakeEmbedder:
    @property
    def dimension(self) -> int:
        return 768

    @property
    def model_name(self) -> str:
        return "fake"

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        return np.zeros((len(texts), 768), dtype=np.float32)

    def embed_query(self, query: str) -> np.ndarray:
        return np.zeros(768, dtype=np.float32)


class TestSingleDocProgressiveInsert:
    """The refactored single-doc path is bounded + progressive (DES-034)."""

    def test_huge_file_flushes_progressively_with_field_parity(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import patch

        from quarry.db.chunk_store import ChunkStore
        from quarry.db.storage import get_db
        from quarry.ingestion.pipeline import ingest_content

        settings = Settings(
            quarry_root=tmp_path / "data",
            lancedb_path=tmp_path / "lancedb",
            registry_path=tmp_path / "registry.db",
            chunk_max_chars=40,
            chunk_overlap_chars=0,
            sync_flush_mb=1,  # ~341 chunks per flush
        )
        db = Database(get_db(settings.lancedb_path))
        text = "The quick brown fox jumps over the lazy dog. " * 800

        orig = ChunkStore.insert_records
        flushes = {"n": 0}

        def counting(self: ChunkStore, records: list[dict[str, object]]) -> int:
            flushes["n"] += 1
            return orig(self, records)

        with (
            patch(
                "quarry.ingestion.streaming.get_embedding_backend",
                return_value=_FakeEmbedder(),
            ),
            patch.object(ChunkStore, "insert_records", counting),
        ):
            result = ingest_content(text, "big", db, settings)

        total = db.store.count()
        assert result["chunks"] == total
        # IngestResult field parity for the inline/content handler.
        assert set(result) == {"document_name", "collection", "chunks", "sections"}
        assert result["document_name"] == "big"
        # Progressive: more than one flush fired for a doc exceeding the budget.
        assert flushes["n"] >= 2
        assert total > 341  # spans multiple flush windows


class TestDeterminismFlag:
    """_pages_are_deterministic keys off OCR (IMAGE) pages, not the file suffix."""

    def _page(self, page_type: PageType) -> PageContent:
        return PageContent(
            document_name="d",
            document_path="/d",
            page_number=1,
            total_pages=1,
            text="text",
            page_type=page_type,
        )

    def test_text_pages_are_deterministic(self) -> None:
        from quarry.ingestion.pipeline import _pages_are_deterministic

        pages = [self._page(PageType.TEXT), self._page(PageType.SECTION)]
        assert _pages_are_deterministic(pages) is True

    def test_any_ocr_image_page_is_non_deterministic(self) -> None:
        from quarry.ingestion.pipeline import _pages_are_deterministic

        # A scanned/OCR'd PDF mixes text and IMAGE pages — one IMAGE page taints it.
        pages = [self._page(PageType.TEXT), self._page(PageType.IMAGE)]
        assert _pages_are_deterministic(pages) is False

    def test_empty_extraction_is_deterministic(self) -> None:
        from quarry.ingestion.pipeline import _pages_are_deterministic

        assert _pages_are_deterministic([]) is True
