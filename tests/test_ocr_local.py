from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import fitz
import pytest
from PIL import Image

import quarry.ocr_local as ocr_local_mod
from quarry.config import Settings
from quarry.models import PageType
from quarry.ocr_local import (
    LocalOcrBackend,
    _extract_text,
    _get_engine,
    _render_pdf_page,
)


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "aws_access_key_id": "test",
        "aws_secret_access_key": "test",
        "textract_poll_initial": 0,
        "textract_max_wait": 1,
    }
    defaults.update(overrides)
    return Settings.model_validate(defaults)


def _mock_ocr_result(texts: list[str] | None) -> SimpleNamespace:
    """Create a mock RapidOCROutput with the given text lines."""
    if texts is None:
        return SimpleNamespace(txts=None, scores=None)
    scores = tuple(0.95 for _ in texts)
    return SimpleNamespace(txts=tuple(texts), scores=scores)


def _create_pdf(tmp_path: Path, text: str, num_pages: int = 1) -> Path:
    """Create a minimal PDF with text on each page."""
    pdf_path = tmp_path / "test.pdf"
    doc = fitz.open()
    for i in range(num_pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"{text} page {i + 1}")
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


def _create_tiff(tmp_path: Path, num_frames: int = 1) -> Path:
    """Create a multi-frame TIFF image."""
    tiff_path = tmp_path / "test.tiff"
    frames = [
        Image.new("RGB", (100, 100), color=(i * 50, 0, 0)) for i in range(num_frames)
    ]
    frames[0].save(tiff_path, save_all=True, append_images=frames[1:])
    return tiff_path


def _create_png_bytes() -> bytes:
    """Create a minimal PNG image as bytes."""
    img = Image.new("RGB", (100, 50), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _reset_engine() -> None:
    """Reset singleton engine between tests."""
    ocr_local_mod._engine = None


class TestExtractText:
    def test_extracts_lines(self) -> None:
        result = _mock_ocr_result(["Hello", "World"])
        assert _extract_text(result) == "Hello\nWorld"

    def test_returns_empty_for_none(self) -> None:
        result = _mock_ocr_result(None)
        assert _extract_text(result) == ""

    def test_single_line(self) -> None:
        result = _mock_ocr_result(["Only line"])
        assert _extract_text(result) == "Only line"


class TestRenderPdfPage:
    def test_renders_to_pil_image(self, tmp_path: Path) -> None:
        pdf_path = _create_pdf(tmp_path, "test")
        with fitz.open(pdf_path) as doc:
            img = _render_pdf_page(doc, 1)
        assert isinstance(img, Image.Image)
        assert img.mode == "RGB"
        assert img.width > 0
        assert img.height > 0

    def test_page_number_is_one_indexed(self, tmp_path: Path) -> None:
        pdf_path = _create_pdf(tmp_path, "test", num_pages=3)
        with fitz.open(pdf_path) as doc:
            img1 = _render_pdf_page(doc, 1)
            img3 = _render_pdf_page(doc, 3)
        assert isinstance(img1, Image.Image)
        assert isinstance(img3, Image.Image)


class TestLocalOcrBackendPdf:
    def test_returns_page_content_per_page(self, tmp_path: Path) -> None:
        pdf_path = _create_pdf(tmp_path, "hello", num_pages=3)
        mock_engine = MagicMock(
            side_effect=[
                _mock_ocr_result(["line A"]),
                _mock_ocr_result(["line B"]),
            ]
        )

        with patch.object(ocr_local_mod, "_get_engine", return_value=mock_engine):
            backend = LocalOcrBackend(_settings())
            results = backend.ocr_document(pdf_path, [1, 3], 3, document_name="doc.pdf")

        assert len(results) == 2
        assert results[0].page_number == 1
        assert results[0].text == "line A"
        assert results[0].total_pages == 3
        assert results[0].document_name == "doc.pdf"
        assert results[0].page_type == PageType.IMAGE
        assert results[1].page_number == 3
        assert results[1].text == "line B"

    def test_handles_no_text_detected(self, tmp_path: Path) -> None:
        pdf_path = _create_pdf(tmp_path, "blank", num_pages=1)
        mock_engine = MagicMock(return_value=_mock_ocr_result(None))

        with patch.object(ocr_local_mod, "_get_engine", return_value=mock_engine):
            backend = LocalOcrBackend(_settings())
            results = backend.ocr_document(pdf_path, [1], 1, document_name="blank.pdf")

        assert len(results) == 1
        assert results[0].text == ""

    def test_document_path_is_resolved(self, tmp_path: Path) -> None:
        pdf_path = _create_pdf(tmp_path, "test")
        mock_engine = MagicMock(return_value=_mock_ocr_result(["text"]))

        with patch.object(ocr_local_mod, "_get_engine", return_value=mock_engine):
            backend = LocalOcrBackend(_settings())
            results = backend.ocr_document(pdf_path, [1], 1, document_name="test.pdf")

        assert results[0].document_path == str(pdf_path.resolve())

    def test_uses_filename_when_no_name(self, tmp_path: Path) -> None:
        pdf_path = _create_pdf(tmp_path, "hello")
        mock_engine = MagicMock(return_value=_mock_ocr_result(["text"]))

        with patch.object(ocr_local_mod, "_get_engine", return_value=mock_engine):
            backend = LocalOcrBackend(_settings())
            results = backend.ocr_document(pdf_path, [1], 1)

        assert results[0].document_name == "test.pdf"

    def test_rejects_unsupported_extension(self, tmp_path: Path) -> None:
        docx_path = tmp_path / "file.docx"
        docx_path.write_bytes(b"fake")
        backend = LocalOcrBackend(_settings())
        with pytest.raises(ValueError, match="Unsupported document type"):
            backend.ocr_document(docx_path, [1], 1)


class TestLocalOcrBackendTiff:
    def test_returns_page_content_per_frame(self, tmp_path: Path) -> None:
        tiff_path = _create_tiff(tmp_path, num_frames=3)
        mock_engine = MagicMock(
            side_effect=[
                _mock_ocr_result(["frame 1"]),
                _mock_ocr_result(["frame 2"]),
            ]
        )

        with patch.object(ocr_local_mod, "_get_engine", return_value=mock_engine):
            backend = LocalOcrBackend(_settings())
            results = backend.ocr_document(
                tiff_path, [1, 3], 3, document_name="scan.tiff"
            )

        assert len(results) == 2
        assert results[0].page_number == 1
        assert results[0].text == "frame 1"
        assert results[1].page_number == 3
        assert results[1].text == "frame 2"

    def test_routes_tif_extension(self, tmp_path: Path) -> None:
        tiff_path = _create_tiff(tmp_path, num_frames=1)
        tif_path = tiff_path.rename(tmp_path / "scan.tif")
        mock_engine = MagicMock(return_value=_mock_ocr_result(["text"]))

        with patch.object(ocr_local_mod, "_get_engine", return_value=mock_engine):
            backend = LocalOcrBackend(_settings())
            results = backend.ocr_document(tif_path, [1], 1, document_name="scan.tif")

        assert len(results) == 1
        assert results[0].document_name == "scan.tif"

    def test_handles_no_text_detected(self, tmp_path: Path) -> None:
        tiff_path = _create_tiff(tmp_path, num_frames=1)
        mock_engine = MagicMock(return_value=_mock_ocr_result(None))

        with patch.object(ocr_local_mod, "_get_engine", return_value=mock_engine):
            backend = LocalOcrBackend(_settings())
            results = backend.ocr_document(
                tiff_path, [1], 1, document_name="blank.tiff"
            )

        assert len(results) == 1
        assert results[0].text == ""


class TestLocalOcrBackendImageBytes:
    def test_returns_single_page_content(self) -> None:
        png_bytes = _create_png_bytes()
        mock_engine = MagicMock(return_value=_mock_ocr_result(["detected text"]))

        with patch.object(ocr_local_mod, "_get_engine", return_value=mock_engine):
            backend = LocalOcrBackend(_settings())
            result = backend.ocr_image_bytes(png_bytes, "img.png", "/tmp/img.png")

        assert result.document_name == "img.png"
        assert result.document_path == "/tmp/img.png"
        assert result.page_number == 1
        assert result.total_pages == 1
        assert result.text == "detected text"
        assert result.page_type == PageType.IMAGE

    def test_handles_no_text(self) -> None:
        png_bytes = _create_png_bytes()
        mock_engine = MagicMock(return_value=_mock_ocr_result(None))

        with patch.object(ocr_local_mod, "_get_engine", return_value=mock_engine):
            backend = LocalOcrBackend(_settings())
            result = backend.ocr_image_bytes(png_bytes, "blank.png", "/tmp/blank.png")

        assert result.text == ""


class TestGetEngine:
    def test_caches_engine(self) -> None:
        mock_cls = MagicMock()
        mock_module = MagicMock()
        mock_module.RapidOCR = mock_cls

        with patch.dict("sys.modules", {"rapidocr": mock_module}):
            first = _get_engine()
            second = _get_engine()

        assert first is second
        mock_cls.assert_called_once()
