from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from quarry.image_analyzer import (
    SUPPORTED_IMAGE_EXTENSIONS,
    ImageAnalysis,
    analyze_image,
)


def _create_image(path: Path, fmt: str) -> None:
    """Create a minimal 1x1 test image."""
    img = Image.new("RGB", (1, 1), color=(0, 0, 0))
    img.save(path, format=fmt)


def _create_multi_page_tiff(path: Path, page_count: int) -> None:
    """Create a multi-page TIFF with the given number of frames."""
    frames = [Image.new("RGB", (1, 1), color=(i, i, i)) for i in range(page_count)]
    frames[0].save(path, format="TIFF", save_all=True, append_images=frames[1:])


class TestAnalyzeImage:
    def test_png(self, tmp_path: Path) -> None:
        path = tmp_path / "test.png"
        _create_image(path, "PNG")
        result = analyze_image(path)
        assert result == ImageAnalysis(
            format="PNG", page_count=1, needs_conversion=False
        )

    def test_jpeg(self, tmp_path: Path) -> None:
        path = tmp_path / "test.jpg"
        _create_image(path, "JPEG")
        result = analyze_image(path)
        assert result == ImageAnalysis(
            format="JPEG", page_count=1, needs_conversion=False
        )

    def test_tiff_single_page(self, tmp_path: Path) -> None:
        path = tmp_path / "test.tiff"
        _create_image(path, "TIFF")
        result = analyze_image(path)
        assert result.format == "TIFF"
        assert result.page_count == 1
        assert result.needs_conversion is False

    def test_tiff_multi_page(self, tmp_path: Path) -> None:
        path = tmp_path / "scan.tiff"
        _create_multi_page_tiff(path, 5)
        result = analyze_image(path)
        assert result == ImageAnalysis(
            format="TIFF", page_count=5, needs_conversion=False
        )

    def test_bmp_needs_conversion(self, tmp_path: Path) -> None:
        path = tmp_path / "test.bmp"
        _create_image(path, "BMP")
        result = analyze_image(path)
        assert result == ImageAnalysis(
            format="BMP", page_count=1, needs_conversion=True
        )

    def test_webp_needs_conversion(self, tmp_path: Path) -> None:
        path = tmp_path / "test.webp"
        _create_image(path, "WEBP")
        result = analyze_image(path)
        assert result == ImageAnalysis(
            format="WEBP", page_count=1, needs_conversion=True
        )

    def test_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            analyze_image(Path("/nonexistent/image.png"))

    def test_unsupported_format(self, tmp_path: Path) -> None:
        path = tmp_path / "test.gif"
        img = Image.new("RGB", (1, 1))
        img.save(path, format="GIF")
        with pytest.raises(ValueError, match="Unsupported image format"):
            analyze_image(path)


class TestSupportedExtensions:
    def test_contains_all_expected(self) -> None:
        expected = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
        assert expected == SUPPORTED_IMAGE_EXTENSIONS
