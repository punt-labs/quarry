from __future__ import annotations

import re
from datetime import UTC, datetime

from ocr.models import Chunk, PageContent

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def chunk_pages(
    pages: list[PageContent],
    max_chars: int = 1800,
    overlap_chars: int = 200,
) -> list[Chunk]:
    """Split page contents into overlapping chunks for embedding.

    Uses sentence boundaries when possible. Each chunk preserves
    the full page raw text for LLM reference.

    Args:
        pages: Extracted page contents.
        max_chars: Target max characters per chunk (~450 tokens).
        overlap_chars: Character overlap between consecutive chunks.

    Returns:
        List of Chunk objects ready for embedding.
    """
    now = datetime.now(tz=UTC)
    all_chunks: list[Chunk] = []

    for page in pages:
        text = page.text.strip()
        if not text:
            continue

        text_chunks = _split_text(text, max_chars, overlap_chars)

        for chunk_index, chunk_text in enumerate(text_chunks):
            all_chunks.append(
                Chunk(
                    document_name=page.document_name,
                    document_path=page.document_path,
                    page_number=page.page_number,
                    total_pages=page.total_pages,
                    chunk_index=chunk_index,
                    text=chunk_text,
                    page_raw_text=page.text,
                    ingestion_timestamp=now,
                )
            )

    return all_chunks


def _split_text(
    text: str,
    max_chars: int,
    overlap_chars: int,
) -> list[str]:
    """Split text into chunks at sentence boundaries with overlap."""
    if len(text) <= max_chars:
        return [text]

    sentences = SENTENCE_SPLIT.split(text)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for sentence in sentences:
        sentence_len = len(sentence)

        if current_len + sentence_len > max_chars and current:
            chunks.append(" ".join(current))
            overlap_text = " ".join(current)
            # Keep tail of previous chunk as overlap
            if len(overlap_text) > overlap_chars:
                tail = overlap_text[-overlap_chars:]
                space_idx = tail.find(" ")
                if space_idx != -1:
                    tail = tail[space_idx + 1 :]
                current = [tail]
                current_len = len(tail)
            else:
                current = []
                current_len = 0

        current.append(sentence)
        current_len += sentence_len + 1  # +1 for space

    if current:
        chunks.append(" ".join(current))

    return chunks
