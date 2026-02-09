from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import cast

import boto3

from quarry.config import Settings
from quarry.models import PageContent, PageType
from quarry.types import S3Client, TextractClient

logger = logging.getLogger(__name__)


def ocr_document_via_s3(
    document_path: Path,
    page_numbers: list[int],
    total_pages: int,
    settings: Settings,
) -> list[PageContent]:
    """OCR document pages using AWS Textract async API via S3.

    Uploads the document to S3, runs Textract text detection,
    then extracts text for the requested pages.
    Supports PDF and multi-page TIFF.

    Args:
        document_path: Path to the document file (PDF or TIFF).
        page_numbers: 1-indexed page numbers that need OCR.
        total_pages: Total pages in the document.
        settings: Application settings with AWS config.

    Returns:
        List of PageContent for each requested page.

    Raises:
        RuntimeError: If the Textract job fails.
        TimeoutError: If the Textract job exceeds the configured timeout.
    """
    s3 = cast("S3Client", boto3.client("s3"))
    textract = cast("TextractClient", boto3.client("textract"))

    s3_key = f"textract-jobs/{document_path.stem}/{document_path.name}"

    logger.info(
        "Uploading %s to s3://%s/%s",
        document_path.name,
        settings.s3_bucket,
        s3_key,
    )
    s3.upload_file(str(document_path), settings.s3_bucket, s3_key)

    try:
        page_texts = _run_textract(
            textract, settings.s3_bucket, s3_key, total_pages, settings
        )
    finally:
        s3.delete_object(Bucket=settings.s3_bucket, Key=s3_key)
        logger.info("Cleaned up S3 object: %s", s3_key)

    requested = set(page_numbers)
    results: list[PageContent] = []
    for page_num, text in sorted(page_texts.items()):
        if page_num in requested:
            results.append(
                PageContent(
                    document_name=document_path.name,
                    document_path=str(document_path.resolve()),
                    page_number=page_num,
                    total_pages=total_pages,
                    text=text,
                    page_type=PageType.IMAGE,
                )
            )

    return results


def ocr_image_bytes(
    image_bytes: bytes,
    document_name: str,
    document_path: str,
) -> PageContent:
    """OCR a single-page image using Textract sync API.

    Uses DetectDocumentText which accepts bytes directly (no S3 needed).

    Args:
        image_bytes: Image file bytes (JPEG or PNG).
        document_name: Document name for metadata.
        document_path: Full path string for metadata.

    Returns:
        PageContent for the single page.

    Raises:
        RuntimeError: If Textract returns no text blocks.
    """
    textract = cast("TextractClient", boto3.client("textract"))

    logger.info("Running sync OCR on %s (%d bytes)", document_name, len(image_bytes))
    response = textract.detect_document_text(
        Document={"Bytes": image_bytes},
    )

    text = _extract_lines_from_blocks(response)
    logger.info("Sync OCR complete for %s: %d chars", document_name, len(text))

    return PageContent(
        document_name=document_name,
        document_path=document_path,
        page_number=1,
        total_pages=1,
        text=text,
        page_type=PageType.IMAGE,
    )


def _extract_lines_from_blocks(response: dict[str, object]) -> str:
    """Extract LINE block text from a Textract response.

    Works for both sync and async responses.

    Returns:
        Lines joined by newlines.
    """
    blocks = response.get("Blocks", [])
    if not isinstance(blocks, list):
        return ""
    return "\n".join(
        str(block["Text"])
        for block in blocks
        if isinstance(block, dict) and block.get("BlockType") == "LINE"
    )


def _run_textract(
    textract: TextractClient,
    bucket: str,
    s3_key: str,
    total_pages: int,
    settings: Settings,
) -> dict[int, str]:
    """Start Textract async job and poll until complete.

    Returns:
        Dict mapping 1-indexed page numbers to extracted text.

    Raises:
        RuntimeError: If the Textract job reports FAILED status.
        TimeoutError: If polling exceeds the configured maximum wait.
    """
    response = textract.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": s3_key}}
    )
    job_id = str(response["JobId"])
    logger.info("Textract job started: %s (%d pages)", job_id, total_pages)

    elapsed = 0
    while elapsed < settings.textract_max_wait:
        time.sleep(settings.textract_poll_interval)
        elapsed += settings.textract_poll_interval

        result = textract.get_document_text_detection(JobId=job_id)
        status = str(result["JobStatus"])

        if status == "SUCCEEDED":
            logger.info("Textract job completed: %s", job_id)
            return _parse_textract_results(textract, job_id, result)

        if status == "FAILED":
            message = result.get("StatusMessage", "Unknown error")
            msg = f"Textract job {job_id} failed: {message}"
            raise RuntimeError(msg)

        logger.info("Textract job %s: %s (%ds elapsed)", job_id, status, elapsed)

    msg = f"Textract job {job_id} timed out after {settings.textract_max_wait}s"
    raise TimeoutError(msg)


def _parse_textract_results(
    textract: TextractClient,
    job_id: str,
    first_response: dict[str, object],
) -> dict[int, str]:
    """Parse all pages of Textract results, handling pagination.

    Returns:
        Dict mapping 1-indexed page numbers to extracted text.
    """
    page_texts: dict[int, list[str]] = {}

    response: dict[str, object] = first_response
    while True:
        blocks = response.get("Blocks", [])
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("BlockType") == "LINE":
                    page_num = int(block["Page"])
                    if page_num not in page_texts:
                        page_texts[page_num] = []
                    page_texts[page_num].append(str(block["Text"]))

        next_token = response.get("NextToken")
        if not next_token:
            break

        response = textract.get_document_text_detection(
            JobId=job_id, NextToken=str(next_token)
        )

    return {page: "\n".join(lines) for page, lines in page_texts.items()}
