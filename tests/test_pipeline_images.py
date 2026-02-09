from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import numpy as np
from PIL import Image

from quarry.config import Settings
from quarry.models import Chunk, PageContent, PageType

if TYPE_CHECKING:
    import pytest


def _settings() -> Settings:
    return Settings(
        aws_access_key_id="test",
        aws_secret_access_key="test",
        textract_poll_initial=0,
    )


def _make_chunks(document_name: str, path: str) -> list[Chunk]:
    return [
        Chunk(
            document_name=document_name,
            document_path=path,
            collection="default",
            page_number=1,
            total_pages=1,
            chunk_index=0,
            text="ocr text",
            page_raw_text="ocr text",
            ingestion_timestamp=datetime.now(tz=UTC),
        )
    ]


def _create_image(path: Path, fmt: str) -> None:
    img = Image.new("RGB", (1, 1), color=(0, 0, 0))
    img.save(path, format=fmt)


def _create_mpo_image(path: Path) -> None:
    """Create a minimal MPO (multi-picture) test image."""
    img1 = Image.new("RGB", (1, 1), color=(255, 0, 0))
    img2 = Image.new("RGB", (1, 1), color=(0, 255, 0))
    img1.save(path, format="MPO", save_all=True, append_images=[img2])


def _create_multi_page_tiff(path: Path, page_count: int) -> None:
    frames = [Image.new("RGB", (1, 1), color=(i, i, i)) for i in range(page_count)]
    frames[0].save(path, format="TIFF", save_all=True, append_images=frames[1:])


def _mock_ocr_backend_single(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock get_ocr_backend for single-page image tests."""
    backend = MagicMock()
    backend.ocr_image_bytes.side_effect = (
        lambda image_bytes, document_name, document_path: PageContent(
            document_name=document_name,
            document_path=document_path,
            page_number=1,
            total_pages=1,
            text="ocr text",
            page_type=PageType.IMAGE,
        )
    )
    monkeypatch.setattr("quarry.pipeline.get_ocr_backend", lambda _settings: backend)
    return backend


def _mock_single_page_pipeline(
    monkeypatch: pytest.MonkeyPatch, chunks: list[Chunk]
) -> None:
    """Set up monkeypatches for single-page image ingestion."""
    vectors = np.zeros((len(chunks), 768), dtype=np.float32)

    _mock_ocr_backend_single(monkeypatch)
    monkeypatch.setattr(
        "quarry.pipeline.chunk_pages",
        lambda _pages, max_chars, overlap_chars, **_kw: chunks,
    )
    embedding_backend = MagicMock()
    embedding_backend.embed_texts.return_value = vectors
    embedding_backend.model_name = "test-model"
    monkeypatch.setattr(
        "quarry.pipeline.get_embedding_backend",
        lambda _settings: embedding_backend,
    )
    monkeypatch.setattr(
        "quarry.pipeline.insert_chunks",
        lambda _db, _chunks, _vectors: len(chunks),
    )


class TestIngestImageSinglePage:
    def test_png(self, monkeypatch, tmp_path: Path) -> None:
        png_file = tmp_path / "photo.png"
        _create_image(png_file, "PNG")

        chunks = _make_chunks("photo.png", str(png_file))
        _mock_single_page_pipeline(monkeypatch, chunks)

        from quarry.pipeline import ingest_document

        db = MagicMock()
        result = ingest_document(png_file, db, _settings())

        assert result["document_name"] == "photo.png"
        assert result["chunks"] == 1
        assert result["format"] == "PNG"
        assert result["image_pages"] == 1

    def test_jpeg(self, monkeypatch, tmp_path: Path) -> None:
        jpg_file = tmp_path / "photo.jpg"
        _create_image(jpg_file, "JPEG")

        chunks = _make_chunks("photo.jpg", str(jpg_file))
        _mock_single_page_pipeline(monkeypatch, chunks)

        from quarry.pipeline import ingest_document

        db = MagicMock()
        result = ingest_document(jpg_file, db, _settings())

        assert result["document_name"] == "photo.jpg"
        assert result["format"] == "JPEG"

    def test_bmp_conversion(self, monkeypatch, tmp_path: Path) -> None:
        bmp_file = tmp_path / "scan.bmp"
        _create_image(bmp_file, "BMP")

        ocr_calls: list[bytes] = []
        backend = MagicMock()

        def _ocr_bytes(
            image_bytes: bytes, document_name: str, document_path: str
        ) -> PageContent:
            ocr_calls.append(image_bytes)
            return PageContent(
                document_name=document_name,
                document_path=document_path,
                page_number=1,
                total_pages=1,
                text="ocr text",
                page_type=PageType.IMAGE,
            )

        backend.ocr_image_bytes.side_effect = _ocr_bytes
        monkeypatch.setattr(
            "quarry.pipeline.get_ocr_backend", lambda _settings: backend
        )
        monkeypatch.setattr(
            "quarry.pipeline.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )

        from quarry.pipeline import ingest_document

        db = MagicMock()
        result = ingest_document(bmp_file, db, _settings())

        assert result["document_name"] == "scan.bmp"
        assert result["format"] == "BMP"
        # Verify bytes were converted to PNG
        assert len(ocr_calls) == 1
        assert ocr_calls[0][:4] == b"\x89PNG"

    def test_webp_conversion(self, monkeypatch, tmp_path: Path) -> None:
        webp_file = tmp_path / "photo.webp"
        _create_image(webp_file, "WEBP")

        ocr_calls: list[bytes] = []
        backend = MagicMock()

        def _ocr_bytes(
            image_bytes: bytes, document_name: str, document_path: str
        ) -> PageContent:
            ocr_calls.append(image_bytes)
            return PageContent(
                document_name=document_name,
                document_path=document_path,
                page_number=1,
                total_pages=1,
                text="ocr text",
                page_type=PageType.IMAGE,
            )

        backend.ocr_image_bytes.side_effect = _ocr_bytes
        monkeypatch.setattr(
            "quarry.pipeline.get_ocr_backend", lambda _settings: backend
        )
        monkeypatch.setattr(
            "quarry.pipeline.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )

        from quarry.pipeline import ingest_document

        db = MagicMock()
        result = ingest_document(webp_file, db, _settings())

        assert result["format"] == "WEBP"
        assert ocr_calls[0][:4] == b"\x89PNG"

    def test_mpo_conversion(self, monkeypatch, tmp_path: Path) -> None:
        mpo_file = tmp_path / "photo.jpg"
        _create_mpo_image(mpo_file)

        ocr_calls: list[bytes] = []
        backend = MagicMock()

        def _ocr_bytes(
            image_bytes: bytes, document_name: str, document_path: str
        ) -> PageContent:
            ocr_calls.append(image_bytes)
            return PageContent(
                document_name=document_name,
                document_path=document_path,
                page_number=1,
                total_pages=1,
                text="ocr text",
                page_type=PageType.IMAGE,
            )

        backend.ocr_image_bytes.side_effect = _ocr_bytes
        monkeypatch.setattr(
            "quarry.pipeline.get_ocr_backend", lambda _settings: backend
        )
        monkeypatch.setattr(
            "quarry.pipeline.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )

        from quarry.pipeline import ingest_document

        db = MagicMock()
        result = ingest_document(mpo_file, db, _settings())

        assert result["format"] == "MPO"
        # Verify bytes were converted to JPEG (not PNG)
        assert len(ocr_calls) == 1
        assert ocr_calls[0][:2] == b"\xff\xd8"  # JPEG SOI marker

    def test_overwrite_deletes_existing(self, monkeypatch, tmp_path: Path) -> None:
        png_file = tmp_path / "photo.png"
        _create_image(png_file, "PNG")

        _mock_single_page_pipeline(
            monkeypatch, _make_chunks("photo.png", str(png_file))
        )

        delete_calls: list[str] = []

        def _mock_delete(_db: object, name: str, **_kw: object) -> int:
            delete_calls.append(name)
            return 0

        monkeypatch.setattr("quarry.pipeline.delete_document", _mock_delete)

        from quarry.pipeline import ingest_document

        db = MagicMock()
        ingest_document(png_file, db, _settings(), overwrite=True)

        assert delete_calls == ["photo.png"]


class TestIngestImageMultiPage:
    def test_tiff_multi_page(self, monkeypatch, tmp_path: Path) -> None:
        tiff_file = tmp_path / "scan.tiff"
        _create_multi_page_tiff(tiff_file, 3)

        ocr_pages = [
            PageContent(
                document_name="scan.tiff",
                document_path=str(tiff_file),
                page_number=i,
                total_pages=3,
                text=f"page {i} text",
                page_type=PageType.IMAGE,
            )
            for i in range(1, 4)
        ]

        chunks = [
            Chunk(
                document_name="scan.tiff",
                document_path=str(tiff_file),
                collection="default",
                page_number=1,
                total_pages=3,
                chunk_index=0,
                text="page 1 text",
                page_raw_text="page 1 text",
                ingestion_timestamp=datetime.now(tz=UTC),
            )
        ]
        vectors = np.zeros((1, 768), dtype=np.float32)

        ocr_backend = MagicMock()
        ocr_backend.ocr_document.return_value = ocr_pages
        monkeypatch.setattr(
            "quarry.pipeline.get_ocr_backend", lambda _settings: ocr_backend
        )
        monkeypatch.setattr(
            "quarry.pipeline.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: chunks,
        )
        embedding_backend = MagicMock()
        embedding_backend.embed_texts.return_value = vectors
        embedding_backend.model_name = "test-model"
        monkeypatch.setattr(
            "quarry.pipeline.get_embedding_backend",
            lambda _settings: embedding_backend,
        )
        monkeypatch.setattr(
            "quarry.pipeline.insert_chunks",
            lambda _db, _chunks, _vectors: 1,
        )

        from quarry.pipeline import ingest_document

        db = MagicMock()
        result = ingest_document(tiff_file, db, _settings())

        assert result["document_name"] == "scan.tiff"
        assert result["format"] == "TIFF"
        assert result["image_pages"] == 3
        assert result["chunks"] == 1
        # Verify backend's ocr_document was called with all pages
        ocr_backend.ocr_document.assert_called_once()
        call_args = ocr_backend.ocr_document.call_args
        assert call_args[0][1] == [1, 2, 3]


class TestIngestImageProgress:
    def test_progress_messages(self, monkeypatch, tmp_path: Path) -> None:
        png_file = tmp_path / "photo.png"
        _create_image(png_file, "PNG")

        _mock_single_page_pipeline(
            monkeypatch, _make_chunks("photo.png", str(png_file))
        )

        from quarry.pipeline import ingest_document

        messages: list[str] = []
        db = MagicMock()
        ingest_document(png_file, db, _settings(), progress_callback=messages.append)

        assert any("Analyzing image" in m for m in messages)
        assert any("PNG" in m for m in messages)
