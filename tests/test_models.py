from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from quarry.models import Chunk, PageAnalysis, PageContent, PageType


class TestPageType:
    def test_values(self):
        assert PageType.TEXT.value == "text"
        assert PageType.IMAGE.value == "image"


class TestPageAnalysis:
    def test_creation(self):
        pa = PageAnalysis(page_number=1, page_type=PageType.TEXT, text_length=100)
        assert pa.page_number == 1
        assert pa.page_type == PageType.TEXT
        assert pa.text_length == 100

    def test_frozen(self):
        pa = PageAnalysis(page_number=1, page_type=PageType.TEXT, text_length=100)
        with pytest.raises(dataclasses.FrozenInstanceError):
            pa.page_number = 2  # type: ignore[misc]


class TestPageContent:
    def test_creation(self):
        pc = PageContent(
            document_name="test.pdf",
            document_path="/tmp/test.pdf",
            page_number=3,
            total_pages=10,
            text="hello",
            page_type=PageType.IMAGE,
        )
        assert pc.document_name == "test.pdf"
        assert pc.page_number == 3
        assert pc.total_pages == 10
        assert pc.page_type == PageType.IMAGE

    def test_frozen(self):
        pc = PageContent(
            document_name="test.pdf",
            document_path="/tmp/test.pdf",
            page_number=1,
            total_pages=1,
            text="x",
            page_type=PageType.TEXT,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            pc.text = "y"  # type: ignore[misc]


class TestChunk:
    def test_creation(self):
        now = datetime.now(tz=UTC)
        chunk = Chunk(
            document_name="doc.pdf",
            document_path="/tmp/doc.pdf",
            collection="default",
            page_number=1,
            total_pages=5,
            chunk_index=0,
            text="chunk text",
            page_raw_text="full page text",
            ingestion_timestamp=now,
        )
        assert chunk.collection == "default"
        assert chunk.text == "chunk text"
        assert chunk.page_raw_text == "full page text"
        assert chunk.ingestion_timestamp == now

    def test_frozen(self):
        now = datetime.now(tz=UTC)
        chunk = Chunk(
            document_name="doc.pdf",
            document_path="/tmp/doc.pdf",
            collection="default",
            page_number=1,
            total_pages=5,
            chunk_index=0,
            text="chunk text",
            page_raw_text="full page text",
            ingestion_timestamp=now,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            chunk.chunk_index = 1  # type: ignore[misc]
