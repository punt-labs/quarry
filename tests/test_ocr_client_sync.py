from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from quarry.ocr_client import _extract_lines_from_blocks, ocr_image_bytes

if TYPE_CHECKING:
    import pytest


def _line_block(text: str) -> dict[str, object]:
    return {"BlockType": "LINE", "Page": 1, "Text": text}


class TestExtractLinesFromBlocks:
    def test_extracts_line_blocks(self) -> None:
        response: dict[str, object] = {
            "Blocks": [
                {"BlockType": "PAGE", "Id": "1"},
                _line_block("Hello"),
                _line_block("World"),
                {"BlockType": "WORD", "Text": "ignored"},
            ],
        }
        assert _extract_lines_from_blocks(response) == "Hello\nWorld"

    def test_empty_blocks(self) -> None:
        response: dict[str, object] = {"Blocks": []}
        assert _extract_lines_from_blocks(response) == ""

    def test_no_blocks_key(self) -> None:
        response: dict[str, object] = {}
        assert _extract_lines_from_blocks(response) == ""


class TestOcrImageBytes:
    def test_successful_ocr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_textract = MagicMock()
        mock_textract.detect_document_text.return_value = {
            "Blocks": [
                _line_block("Detected text"),
                _line_block("Second line"),
            ],
        }

        monkeypatch.setattr(
            "quarry.ocr_client.boto3.client",
            lambda service: mock_textract,
        )

        result = ocr_image_bytes(
            image_bytes=b"fake-png-bytes",
            document_name="photo.png",
            document_path="/tmp/photo.png",
        )

        assert result.document_name == "photo.png"
        assert result.document_path == "/tmp/photo.png"
        assert result.text == "Detected text\nSecond line"
        assert result.page_number == 1
        assert result.total_pages == 1

        mock_textract.detect_document_text.assert_called_once_with(
            Document={"Bytes": b"fake-png-bytes"},
        )

    def test_empty_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_textract = MagicMock()
        mock_textract.detect_document_text.return_value = {"Blocks": []}

        monkeypatch.setattr(
            "quarry.ocr_client.boto3.client",
            lambda service: mock_textract,
        )

        result = ocr_image_bytes(
            image_bytes=b"blank-image",
            document_name="blank.png",
            document_path="/tmp/blank.png",
        )

        assert result.text == ""
