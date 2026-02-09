from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from quarry.config import Settings
from quarry.ocr_client import _parse_textract_results, ocr_document_via_s3


def _settings() -> Settings:
    return Settings(
        aws_access_key_id="test",
        aws_secret_access_key="test",
        textract_poll_initial=0,
        textract_max_wait=1,
    )


def _make_textract_response(
    status: str,
    blocks: list[dict[str, object]] | None = None,
    next_token: str | None = None,
) -> dict[str, object]:
    resp: dict[str, object] = {"JobStatus": status}
    if blocks is not None:
        resp["Blocks"] = blocks
    if next_token is not None:
        resp["NextToken"] = next_token
    return resp


def _line_block(page: int, text: str) -> dict[str, object]:
    return {"BlockType": "LINE", "Page": page, "Text": text}


class TestParseTextractResults:
    def test_single_page(self):
        textract = MagicMock()
        response: dict[str, object] = {
            "JobStatus": "SUCCEEDED",
            "Blocks": [
                _line_block(1, "Hello"),
                _line_block(1, "World"),
            ],
        }
        result = _parse_textract_results(textract, "job-1", response)
        assert result == {1: "Hello\nWorld"}

    def test_multiple_pages(self):
        textract = MagicMock()
        response: dict[str, object] = {
            "JobStatus": "SUCCEEDED",
            "Blocks": [
                _line_block(1, "Page one"),
                _line_block(2, "Page two"),
                _line_block(1, "More on one"),
            ],
        }
        result = _parse_textract_results(textract, "job-1", response)
        assert result[1] == "Page one\nMore on one"
        assert result[2] == "Page two"

    def test_pagination(self):
        textract = MagicMock()
        first_response: dict[str, object] = {
            "JobStatus": "SUCCEEDED",
            "Blocks": [_line_block(1, "First batch")],
            "NextToken": "token-2",
        }
        second_response: dict[str, object] = {
            "JobStatus": "SUCCEEDED",
            "Blocks": [_line_block(1, "Second batch")],
        }
        textract.get_document_text_detection.return_value = second_response

        result = _parse_textract_results(textract, "job-1", first_response)
        assert result[1] == "First batch\nSecond batch"
        textract.get_document_text_detection.assert_called_once_with(
            JobId="job-1", NextToken="token-2"
        )

    def test_skips_non_line_blocks(self):
        textract = MagicMock()
        response: dict[str, object] = {
            "JobStatus": "SUCCEEDED",
            "Blocks": [
                {"BlockType": "PAGE", "Page": 1},
                _line_block(1, "Actual text"),
                {"BlockType": "WORD", "Page": 1, "Text": "word"},
            ],
        }
        result = _parse_textract_results(textract, "job-1", response)
        assert result == {1: "Actual text"}


class TestOcrDocumentViaS3:
    def test_successful_flow(self, monkeypatch, tmp_path: Path):
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        mock_s3 = MagicMock()
        mock_textract = MagicMock()

        mock_textract.start_document_text_detection.return_value = {"JobId": "job-123"}
        mock_textract.get_document_text_detection.return_value = {
            "JobStatus": "SUCCEEDED",
            "Blocks": [
                _line_block(1, "OCR text page 1"),
                _line_block(2, "OCR text page 2"),
            ],
        }

        def mock_boto3_client(service: str) -> MagicMock:
            if service == "s3":
                return mock_s3
            return mock_textract

        monkeypatch.setattr("quarry.ocr_client.boto3.client", mock_boto3_client)

        results = ocr_document_via_s3(
            pdf_file,
            page_numbers=[1, 2],
            total_pages=2,
            settings=_settings(),
        )

        assert len(results) == 2
        assert results[0].page_number == 1
        assert results[0].text == "OCR text page 1"
        assert results[1].page_number == 2
        mock_s3.upload_file.assert_called_once()
        mock_s3.delete_object.assert_called_once()

    def test_filters_requested_pages(self, monkeypatch, tmp_path: Path):
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        mock_s3 = MagicMock()
        mock_textract = MagicMock()

        mock_textract.start_document_text_detection.return_value = {"JobId": "job-123"}
        mock_textract.get_document_text_detection.return_value = {
            "JobStatus": "SUCCEEDED",
            "Blocks": [
                _line_block(1, "Page 1"),
                _line_block(2, "Page 2"),
                _line_block(3, "Page 3"),
            ],
        }

        def mock_boto3_client(service: str) -> MagicMock:
            if service == "s3":
                return mock_s3
            return mock_textract

        monkeypatch.setattr("quarry.ocr_client.boto3.client", mock_boto3_client)

        results = ocr_document_via_s3(
            pdf_file,
            page_numbers=[2],
            total_pages=3,
            settings=_settings(),
        )

        assert len(results) == 1
        assert results[0].page_number == 2
        assert results[0].text == "Page 2"

    def test_s3_cleanup_on_textract_failure(self, monkeypatch, tmp_path: Path):
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        mock_s3 = MagicMock()
        mock_textract = MagicMock()

        mock_textract.start_document_text_detection.return_value = {"JobId": "job-fail"}
        mock_textract.get_document_text_detection.return_value = {
            "JobStatus": "FAILED",
            "StatusMessage": "Bad input",
        }

        def mock_boto3_client(service: str) -> MagicMock:
            if service == "s3":
                return mock_s3
            return mock_textract

        monkeypatch.setattr("quarry.ocr_client.boto3.client", mock_boto3_client)

        with pytest.raises(RuntimeError, match="failed: Bad input"):
            ocr_document_via_s3(
                pdf_file,
                page_numbers=[1],
                total_pages=1,
                settings=_settings(),
            )

        # S3 cleanup should still happen
        mock_s3.delete_object.assert_called_once()

    def test_timeout(self, monkeypatch, tmp_path: Path):
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        mock_s3 = MagicMock()
        mock_textract = MagicMock()

        mock_textract.start_document_text_detection.return_value = {"JobId": "job-slow"}
        mock_textract.get_document_text_detection.return_value = {
            "JobStatus": "IN_PROGRESS",
        }

        def mock_boto3_client(service: str) -> MagicMock:
            if service == "s3":
                return mock_s3
            return mock_textract

        monkeypatch.setattr("quarry.ocr_client.boto3.client", mock_boto3_client)

        settings = Settings(
            aws_access_key_id="test",
            aws_secret_access_key="test",
            textract_poll_initial=0,
            textract_max_wait=0,
        )

        with pytest.raises(TimeoutError, match="timed out"):
            ocr_document_via_s3(
                pdf_file,
                page_numbers=[1],
                total_pages=1,
                settings=settings,
            )

        mock_s3.delete_object.assert_called_once()
