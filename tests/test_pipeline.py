from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from quarry.config import Settings
from quarry.models import Chunk, PageAnalysis, PageContent, PageType


def _settings() -> Settings:
    return Settings(
        aws_access_key_id="test",
        aws_secret_access_key="test",
        textract_poll_initial=0,
    )


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


class TestIngestDocument:
    def test_file_not_found(self):
        from quarry.pipeline import ingest_document

        db = MagicMock()
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
                ingestion_timestamp=datetime.now(tz=UTC),
            )
        ]
        vectors = np.zeros((1, 768), dtype=np.float32)

        monkeypatch.setattr("quarry.pipeline.analyze_pdf", lambda _path: analyses)
        monkeypatch.setattr(
            "quarry.pipeline.extract_text_pages",
            lambda _path, _pages, _total, **_kw: text_pages,
        )
        monkeypatch.setattr(
            "quarry.pipeline.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: chunks,
        )
        monkeypatch.setattr(
            "quarry.pipeline.embed_texts",
            lambda _texts, model_name: vectors,
        )
        monkeypatch.setattr(
            "quarry.pipeline.insert_chunks",
            lambda _db, _chunks, _vectors: 1,
        )

        from quarry.pipeline import ingest_document

        db = MagicMock()
        result = ingest_document(pdf_file, db, _settings())

        assert result["document_name"] == "test.pdf"
        assert result["total_pages"] == 2
        assert result["text_pages"] == 2
        assert result["image_pages"] == 0
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
                ingestion_timestamp=datetime.now(tz=UTC),
            )
        ]
        vectors = np.zeros((1, 768), dtype=np.float32)

        monkeypatch.setattr("quarry.pipeline.analyze_pdf", lambda _path: analyses)
        monkeypatch.setattr(
            "quarry.pipeline.ocr_document_via_s3",
            lambda _path, _pages, _total, _settings, **_kw: ocr_pages,
        )
        monkeypatch.setattr(
            "quarry.pipeline.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: chunks,
        )
        monkeypatch.setattr(
            "quarry.pipeline.embed_texts",
            lambda _texts, model_name: vectors,
        )
        monkeypatch.setattr(
            "quarry.pipeline.insert_chunks",
            lambda _db, _chunks, _vectors: 1,
        )

        from quarry.pipeline import ingest_document

        db = MagicMock()
        result = ingest_document(pdf_file, db, _settings())

        assert result["image_pages"] == 1
        assert result["text_pages"] == 0

    def test_no_text_extracted(self, monkeypatch, tmp_path: Path):
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        analyses = [
            PageAnalysis(page_number=1, page_type=PageType.IMAGE, text_length=0),
        ]
        ocr_pages = [_make_page_content(1, PageType.IMAGE, "")]

        monkeypatch.setattr("quarry.pipeline.analyze_pdf", lambda _path: analyses)
        monkeypatch.setattr(
            "quarry.pipeline.ocr_document_via_s3",
            lambda _path, _pages, _total, _settings, **_kw: ocr_pages,
        )
        monkeypatch.setattr(
            "quarry.pipeline.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )

        from quarry.pipeline import ingest_document

        db = MagicMock()
        result = ingest_document(pdf_file, db, _settings())

        assert result["chunks"] == 0

    def test_overwrite_deletes_existing(self, monkeypatch, tmp_path: Path):
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        analyses = [
            PageAnalysis(page_number=1, page_type=PageType.TEXT, text_length=100),
        ]
        text_pages = [_make_page_content(1, PageType.TEXT)]

        monkeypatch.setattr("quarry.pipeline.analyze_pdf", lambda _path: analyses)
        monkeypatch.setattr(
            "quarry.pipeline.extract_text_pages",
            lambda _path, _pages, _total, **_kw: text_pages,
        )
        monkeypatch.setattr(
            "quarry.pipeline.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )

        delete_called_with: list[str] = []

        def _mock_delete(_db: object, name: str, **_kw: object) -> int:
            delete_called_with.append(name)
            return 0

        monkeypatch.setattr("quarry.pipeline.delete_document", _mock_delete)

        from quarry.pipeline import ingest_document

        db = MagicMock()
        ingest_document(pdf_file, db, _settings(), overwrite=True)

        assert delete_called_with == ["test.pdf"]

    def test_progress_callback(self, monkeypatch, tmp_path: Path):
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        monkeypatch.setattr(
            "quarry.pipeline.analyze_pdf",
            lambda _path: [
                PageAnalysis(page_number=1, page_type=PageType.TEXT, text_length=100)
            ],
        )
        monkeypatch.setattr(
            "quarry.pipeline.extract_text_pages",
            lambda _path, _pages, _total, **_kw: [_make_page_content(1, PageType.TEXT)],
        )
        monkeypatch.setattr(
            "quarry.pipeline.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )

        from quarry.pipeline import ingest_document

        messages: list[str] = []
        db = MagicMock()
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
                ingestion_timestamp=datetime.now(tz=UTC),
            )
        ]
        vectors = np.zeros((1, 768), dtype=np.float32)

        monkeypatch.setattr(
            "quarry.pipeline.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: chunks,
        )
        monkeypatch.setattr(
            "quarry.pipeline.embed_texts",
            lambda _texts, model_name: vectors,
        )
        monkeypatch.setattr(
            "quarry.pipeline.insert_chunks",
            lambda _db, _chunks, _vectors: 1,
        )

        from quarry.pipeline import ingest_document

        db = MagicMock()
        result = ingest_document(txt_file, db, _settings())

        assert result["document_name"] == "notes.txt"
        assert result["chunks"] == 1
        assert result["sections"] == 2

    def test_unsupported_format_raises(self, tmp_path: Path):
        csv_file = tmp_path / "data.csv"
        csv_file.write_text("a,b,c")

        from quarry.pipeline import ingest_document

        db = MagicMock()
        with pytest.raises(ValueError, match="Unsupported file format"):
            ingest_document(csv_file, db, _settings())


class TestIngestText:
    def test_ingests_raw_text(self, monkeypatch):
        chunks = [
            Chunk(
                document_name="clip.txt",
                document_path="<string>",
                collection="default",
                page_number=1,
                total_pages=1,
                chunk_index=0,
                text="Hello world",
                page_raw_text="Hello world",
                ingestion_timestamp=datetime.now(tz=UTC),
            )
        ]
        vectors = np.zeros((1, 768), dtype=np.float32)

        monkeypatch.setattr(
            "quarry.pipeline.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: chunks,
        )
        monkeypatch.setattr(
            "quarry.pipeline.embed_texts",
            lambda _texts, model_name: vectors,
        )
        monkeypatch.setattr(
            "quarry.pipeline.insert_chunks",
            lambda _db, _chunks, _vectors: 1,
        )

        from quarry.pipeline import ingest_text

        db = MagicMock()
        result = ingest_text("Hello world", "clip.txt", db, _settings())

        assert result["document_name"] == "clip.txt"
        assert result["chunks"] == 1
        assert result["sections"] == 1

    def test_overwrite_deletes_existing(self, monkeypatch):
        monkeypatch.setattr(
            "quarry.pipeline.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )

        delete_called: list[str] = []

        def _mock_delete(_db: object, name: str, **_kw: object) -> int:
            delete_called.append(name)
            return 0

        monkeypatch.setattr("quarry.pipeline.delete_document", _mock_delete)

        from quarry.pipeline import ingest_text

        db = MagicMock()
        ingest_text("text", "doc.txt", db, _settings(), overwrite=True)

        assert delete_called == ["doc.txt"]

    def test_empty_text_returns_zero_chunks(self, monkeypatch):
        monkeypatch.setattr(
            "quarry.pipeline.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )

        from quarry.pipeline import ingest_text

        db = MagicMock()
        result = ingest_text("", "empty.txt", db, _settings())

        assert result["chunks"] == 0
