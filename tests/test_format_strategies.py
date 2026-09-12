"""Tests for quarry.ingestion.format_strategies — per-suffix extraction strategies."""

from __future__ import annotations

from pathlib import Path
from typing import final
from unittest.mock import MagicMock

import pytest
from PIL import Image

from quarry.config import Settings
from quarry.ingestion.format_strategies import (
    SUPPORTED_EXTENSIONS,
    ImageFormatStrategy,
    PdfFormatStrategy,
    TextLikeStrategy,
    resolve_strategy,
)
from quarry.ingestion.ingest_context import Progress
from quarry.ingestion.ingest_stats import IngestStats
from quarry.ingestion.text_format import TextLikeFormat
from quarry.models import PageAnalysis, PageContent, PageType


def _settings() -> Settings:
    return Settings()


@final
class _FakeOcrBackend:
    """A minimal OcrBackend stand-in, injected via the strategy's constructor."""

    def __init__(self) -> None:
        self.ocr_document_calls: list[tuple[Path, list[int], int]] = []

    def ocr_document(
        self,
        document_path: Path,
        page_numbers: list[int],
        total_pages: int,
        *,
        document_name: str | None = None,
    ) -> list[PageContent]:
        self.ocr_document_calls.append((document_path, page_numbers, total_pages))
        return [
            PageContent(
                document_name=document_name or document_path.name,
                document_path=str(document_path),
                page_number=n,
                total_pages=total_pages,
                text=f"ocr page {n}",
                page_type=PageType.IMAGE,
            )
            for n in page_numbers
        ]

    def ocr_image_bytes(
        self, image_bytes: bytes, document_name: str, document_path: Path
    ) -> PageContent:
        return PageContent(
            document_name=document_name,
            document_path=str(document_path),
            page_number=1,
            total_pages=1,
            text="ocr text",
            page_type=PageType.IMAGE,
        )


def _make_image(path: Path, fmt: str) -> None:
    Image.new("RGB", (1, 1), color=(0, 0, 0)).save(path, format=fmt)


def _make_multipage_tiff(path: Path, page_count: int) -> None:
    frames = [Image.new("RGB", (1, 1), color=(i, i, i)) for i in range(page_count)]
    frames[0].save(path, format="TIFF", save_all=True, append_images=frames[1:])


class TestPdfFormatStrategy:
    def test_extract_reports_page_breakdown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pdf_file = tmp_path / "doc.pdf"
        pdf_file.write_bytes(b"%PDF-fake")
        analyses = [
            PageAnalysis(page_number=1, page_type=PageType.TEXT, text_length=100),
            PageAnalysis(page_number=2, page_type=PageType.IMAGE, text_length=0),
        ]
        text_pages = [
            PageContent("doc.pdf", str(pdf_file), 1, 2, "hello", PageType.TEXT)
        ]
        monkeypatch.setattr(
            "quarry.extractors.pdf_extractor.PdfExtractor._classify_pages",
            staticmethod(lambda _path: analyses),
        )
        monkeypatch.setattr(
            "quarry.extractors.pdf_extractor.extract_text_pages",
            lambda _path, _pages, _total, **_kw: text_pages,
        )
        # The fake OcrBackend is injected via the constructor, so PdfExtractor's
        # image-page branch calls it directly -- no module patch needed.
        ocr = _FakeOcrBackend()

        strategy = PdfFormatStrategy(ocr)
        messages: list[str] = []
        extracted = strategy.extract(
            _settings(), pdf_file, "doc.pdf", Progress(messages.append)
        )

        assert extracted.source_format == ".pdf"
        assert len(extracted.pages) == 2
        assert extracted.stats == IngestStats(
            total_pages=2, text_pages=1, image_pages=1
        )
        assert any("Analyzing" in m for m in messages)
        assert any(m.startswith("Pages:") for m in messages)


class TestImageFormatStrategy:
    def test_single_page_png(self, tmp_path: Path) -> None:
        png_file = tmp_path / "photo.png"
        _make_image(png_file, "PNG")
        ocr = _FakeOcrBackend()

        strategy = ImageFormatStrategy(ocr)
        extracted = strategy.extract(_settings(), png_file, "photo.png", Progress(None))

        assert extracted.source_format == ".png"
        assert len(extracted.pages) == 1
        assert extracted.pages[0].text == "ocr text"
        assert extracted.stats.image_pages == 1

    def test_multipage_tiff_uses_async_ocr(self, tmp_path: Path) -> None:
        tiff_file = tmp_path / "scan.tiff"
        _make_multipage_tiff(tiff_file, 3)
        ocr = _FakeOcrBackend()

        strategy = ImageFormatStrategy(ocr)
        extracted = strategy.extract(
            _settings(), tiff_file, "scan.tiff", Progress(None)
        )

        assert len(extracted.pages) == 3
        assert extracted.stats.file_format == "TIFF"
        assert extracted.stats.image_pages == 3
        assert ocr.ocr_document_calls[0][1] == [1, 2, 3]

    def test_bmp_requires_conversion_before_ocr(self, tmp_path: Path) -> None:
        bmp_file = tmp_path / "scan.bmp"
        _make_image(bmp_file, "BMP")
        ocr = _FakeOcrBackend()

        strategy = ImageFormatStrategy(ocr)
        extracted = strategy.extract(_settings(), bmp_file, "scan.bmp", Progress(None))

        assert extracted.source_format == ".bmp"
        assert len(extracted.pages) == 1


class TestTextLikeStrategy:
    def test_delegates_extract_and_stats_to_the_format(self, tmp_path: Path) -> None:
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("hi")
        pages = [PageContent("notes.txt", str(txt_file), 1, 1, "hi", PageType.SECTION)]
        fmt = TextLikeFormat(
            read_verb="Reading",
            unit_label="Sections",
            make_extractor=lambda _s: MagicMock(extract_pages=lambda *_a, **_k: pages),
            make_stats=lambda n: IngestStats(sections=n),
        )
        messages: list[str] = []

        strategy = TextLikeStrategy(fmt)
        extracted = strategy.extract(
            _settings(), txt_file, "notes.txt", Progress(messages.append)
        )

        assert extracted.pages == pages
        assert extracted.stats == IngestStats(sections=1)
        assert extracted.source_format == ".txt"
        assert any("Reading" in m for m in messages)
        assert any("Sections" in m for m in messages)


class TestResolveStrategy:
    def test_pdf_suffix_resolves_pdf_strategy(self) -> None:
        strategy = resolve_strategy(".pdf", _settings())
        assert isinstance(strategy, PdfFormatStrategy)

    def test_image_suffix_resolves_image_strategy(self) -> None:
        strategy = resolve_strategy(".png", _settings())
        assert isinstance(strategy, ImageFormatStrategy)

    def test_text_like_suffix_resolves_text_like_strategy(self) -> None:
        strategy = resolve_strategy(".txt", _settings())
        assert isinstance(strategy, TextLikeStrategy)

    def test_unsupported_suffix_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported file format"):
            resolve_strategy(".zip", _settings())

    def test_supported_extensions_covers_pdf_image_and_text(self) -> None:
        assert ".pdf" in SUPPORTED_EXTENSIONS
        assert ".png" in SUPPORTED_EXTENSIONS
        assert ".txt" in SUPPORTED_EXTENSIONS
