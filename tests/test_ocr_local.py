from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import pymupdf
import pytest
from PIL import Image

from quarry.config import Settings
from quarry.ingestion.ocr_availability import OcrUnavailableError
from quarry.ingestion.ocr_engine import OcrEngine
from quarry.ingestion.ocr_local import LocalOcrBackend
from quarry.models import PageType

if TYPE_CHECKING:
    from quarry.ingestion.ocr_engine import OcrResult


def _settings(**overrides: object) -> Settings:
    return Settings.model_validate(overrides)


def _mock_ocr_result(texts: list[str] | None) -> OcrResult:
    """Create a mock RapidOCROutput with the given text lines."""
    if texts is None:
        return cast("OcrResult", SimpleNamespace(txts=None, scores=None))
    scores = tuple(0.95 for _ in texts)
    return cast("OcrResult", SimpleNamespace(txts=tuple(texts), scores=scores))


def _create_pdf(tmp_path: Path, text: str, num_pages: int = 1) -> Path:
    """Create a minimal PDF with text on each page."""
    pdf_path = tmp_path / "test.pdf"
    doc = pymupdf.open()
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
    """Reset singleton engine and the OCR-unavailable warn latch between tests."""
    OcrEngine.reset()
    LocalOcrBackend._warned_unavailable = False


class TestExtractText:
    def test_extracts_lines(self) -> None:
        result = _mock_ocr_result(["Hello", "World"])
        assert LocalOcrBackend._extract_text(result) == "Hello\nWorld"

    def test_returns_empty_for_none(self) -> None:
        result = _mock_ocr_result(None)
        assert LocalOcrBackend._extract_text(result) == ""

    def test_single_line(self) -> None:
        result = _mock_ocr_result(["Only line"])
        assert LocalOcrBackend._extract_text(result) == "Only line"


class TestRenderPdfPage:
    def test_renders_to_pil_image(self, tmp_path: Path) -> None:
        pdf_path = _create_pdf(tmp_path, "test")
        with pymupdf.open(pdf_path) as doc:
            img = LocalOcrBackend._render_pdf_page(doc, 1)
        assert isinstance(img, Image.Image)
        assert img.mode == "RGB"
        assert img.width > 0
        assert img.height > 0

    def test_page_number_is_one_indexed(self, tmp_path: Path) -> None:
        pdf_path = _create_pdf(tmp_path, "test", num_pages=3)
        with pymupdf.open(pdf_path) as doc:
            img1 = LocalOcrBackend._render_pdf_page(doc, 1)
            img3 = LocalOcrBackend._render_pdf_page(doc, 3)
        assert isinstance(img1, Image.Image)
        assert isinstance(img3, Image.Image)


class TestOcrDocumentFormat:
    @pytest.mark.parametrize(
        "suffix", [".pdf", ".PDF", ".tif", ".TIF", ".tiff", ".TIFF"]
    )
    @pytest.mark.parametrize("page_numbers", [[], [1]])
    def test_supported_formats(
        self, tmp_path: Path, suffix: str, page_numbers: list[int]
    ) -> None:
        if suffix.lower() == ".pdf":
            path = _create_pdf(tmp_path, "scan")
        else:
            path = _create_tiff(tmp_path)
        path = path.rename(tmp_path / f"scan{suffix}")
        engine = MagicMock(return_value=_mock_ocr_result(["text"]))

        with patch.object(OcrEngine, "get", return_value=engine):
            pages = LocalOcrBackend(_settings()).ocr_document(path, page_numbers, 1)

        assert [page.text for page in pages] == ["text"] * len(page_numbers)
        assert engine.call_count == len(page_numbers)

    @pytest.mark.parametrize("filename", ["scan", "scan.PNG"])
    def test_invalid_format_rejected_before_engine_lookup(
        self, tmp_path: Path, filename: str
    ) -> None:
        path = tmp_path / filename
        with (
            patch.object(OcrEngine, "get") as lookup,
            pytest.raises(ValueError) as exc_info,
        ):
            LocalOcrBackend(_settings()).ocr_document(path, [], 0)

        assert str(exc_info.value) == (
            f"Unsupported document type for OCR: '{path.suffix.lower()}'"
        )
        lookup.assert_not_called()


class TestLocalOcrBackendPdf:
    def test_returns_page_content_per_page(self, tmp_path: Path) -> None:
        pdf_path = _create_pdf(tmp_path, "hello", num_pages=3)
        mock_engine = MagicMock(
            side_effect=[
                _mock_ocr_result(["line A"]),
                _mock_ocr_result(["line B"]),
            ]
        )

        with patch.object(OcrEngine, "get", return_value=mock_engine):
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

        with patch.object(OcrEngine, "get", return_value=mock_engine):
            backend = LocalOcrBackend(_settings())
            results = backend.ocr_document(pdf_path, [1], 1, document_name="blank.pdf")

        assert len(results) == 1
        assert results[0].text == ""

    def test_document_path_is_resolved(self, tmp_path: Path) -> None:
        pdf_path = _create_pdf(tmp_path, "test")
        mock_engine = MagicMock(return_value=_mock_ocr_result(["text"]))

        with patch.object(OcrEngine, "get", return_value=mock_engine):
            backend = LocalOcrBackend(_settings())
            results = backend.ocr_document(pdf_path, [1], 1, document_name="test.pdf")

        assert results[0].document_path == str(pdf_path.resolve())

    def test_uses_filename_when_no_name(self, tmp_path: Path) -> None:
        pdf_path = _create_pdf(tmp_path, "hello")
        mock_engine = MagicMock(return_value=_mock_ocr_result(["text"]))

        with patch.object(OcrEngine, "get", return_value=mock_engine):
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

        with patch.object(OcrEngine, "get", return_value=mock_engine):
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

        with patch.object(OcrEngine, "get", return_value=mock_engine):
            backend = LocalOcrBackend(_settings())
            results = backend.ocr_document(tif_path, [1], 1, document_name="scan.tif")

        assert len(results) == 1
        assert results[0].document_name == "scan.tif"

    def test_handles_no_text_detected(self, tmp_path: Path) -> None:
        tiff_path = _create_tiff(tmp_path, num_frames=1)
        mock_engine = MagicMock(return_value=_mock_ocr_result(None))

        with patch.object(OcrEngine, "get", return_value=mock_engine):
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

        with patch.object(OcrEngine, "get", return_value=mock_engine):
            backend = LocalOcrBackend(_settings())
            result = backend.ocr_image_bytes(png_bytes, "img.png", Path("/tmp/img.png"))

        assert result.document_name == "img.png"
        assert result.document_path == "/tmp/img.png"
        assert result.page_number == 1
        assert result.total_pages == 1
        assert result.text == "detected text"
        assert result.page_type == PageType.IMAGE

    def test_handles_no_text(self) -> None:
        png_bytes = _create_png_bytes()
        mock_engine = MagicMock(return_value=_mock_ocr_result(None))

        with patch.object(OcrEngine, "get", return_value=mock_engine):
            backend = LocalOcrBackend(_settings())
            result = backend.ocr_image_bytes(
                png_bytes, "blank.png", Path("/tmp/blank.png")
            )

        assert result.text == ""

    def test_opened_image_is_released_via_context_manager(self) -> None:
        # The opened PIL image must be closed deterministically: assert the
        # context-manager protocol runs so Pillow's file-like resources free.
        png_bytes = _create_png_bytes()
        mock_engine = MagicMock(return_value=_mock_ocr_result(["x"]))
        rgb = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        cm = MagicMock()
        cm.__enter__.return_value.convert.return_value = rgb

        with (
            patch.object(OcrEngine, "get", return_value=mock_engine),
            patch("quarry.ingestion.ocr_local.Image.open", return_value=cm),
        ):
            backend = LocalOcrBackend(_settings())
            result = backend.ocr_image_bytes(png_bytes, "img.png", Path("/tmp/img.png"))

        cm.__enter__.assert_called_once()
        cm.__exit__.assert_called_once()
        assert result.text == "x"


class TestOcrDegradesWhenUnavailable:
    """OCR-unavailable (headless cv2 or missing rapidocr) degrades, never crashes."""

    def test_document_returns_no_pages_on_unavailable(self, tmp_path: Path) -> None:
        pdf_path = _create_pdf(tmp_path, "scanned", num_pages=2)
        unavailable = OcrUnavailableError("headless: libGL.so.1 not loadable")

        with patch.object(OcrEngine, "get", side_effect=unavailable):
            backend = LocalOcrBackend(_settings())
            pages = backend.ocr_document(pdf_path, [1, 2], 2, document_name="scan.pdf")

        assert pages == []

    def test_document_returns_no_pages_on_import_error(self, tmp_path: Path) -> None:
        tiff_path = _create_tiff(tmp_path, num_frames=2)

        with patch.object(OcrEngine, "get", side_effect=ImportError("no rapidocr")):
            backend = LocalOcrBackend(_settings())
            pages = backend.ocr_document(tiff_path, [1, 2], 2)

        assert pages == []

    def test_image_bytes_degrades_to_empty_text(self) -> None:
        png_bytes = _create_png_bytes()
        unavailable = OcrUnavailableError("headless: cv2 unavailable")

        with patch.object(OcrEngine, "get", side_effect=unavailable):
            backend = LocalOcrBackend(_settings())
            result = backend.ocr_image_bytes(
                png_bytes, "scan.png", Path("/tmp/scan.png")
            )

        assert result.text == ""
        assert result.document_name == "scan.png"
        assert result.page_type == PageType.IMAGE

    def test_unsupported_extension_still_raises(self, tmp_path: Path) -> None:
        # A real caller error must NOT be swallowed by the unavailability guard.
        docx_path = tmp_path / "file.docx"
        docx_path.write_bytes(b"fake")
        backend = LocalOcrBackend(_settings())
        with pytest.raises(ValueError, match="Unsupported document type"):
            backend.ocr_document(docx_path, [1], 1)

    def test_warns_exactly_once_across_calls(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        pdf_path = _create_pdf(tmp_path, "scanned", num_pages=1)
        png_bytes = _create_png_bytes()
        unavailable = OcrUnavailableError("headless: cv2 unavailable")

        with (
            patch.object(OcrEngine, "get", side_effect=unavailable),
            caplog.at_level("WARNING", logger="quarry.ingestion.ocr_local"),
        ):
            backend = LocalOcrBackend(_settings())
            backend.ocr_document(pdf_path, [1], 1)
            backend.ocr_image_bytes(png_bytes, "scan.png", Path("/tmp/scan.png"))
            backend.ocr_document(pdf_path, [1], 1)

        warnings = [r for r in caplog.records if "OCR unavailable" in r.message]
        assert len(warnings) == 1
