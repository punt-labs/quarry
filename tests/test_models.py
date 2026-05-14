from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from quarry.models import (
    Chunk,
    ChunkConfig,
    PageAnalysis,
    PageContent,
    PageType,
    SitemapOptions,
)


class TestPageType:
    def test_values(self):
        assert PageType.TEXT.value == "text"
        assert PageType.IMAGE.value == "image"


class TestStoredPageType:
    def test_text_maps_to_text(self):
        assert PageType.TEXT.stored == "text"

    def test_image_maps_to_text(self):
        assert PageType.IMAGE.stored == "text"

    def test_section_maps_to_text(self):
        assert PageType.SECTION.stored == "text"

    def test_code_maps_to_code(self):
        assert PageType.CODE.stored == "code"


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
            page_type="text",
            source_format=".pdf",
            ingestion_timestamp=now,
        )
        assert chunk.collection == "default"
        assert chunk.text == "chunk text"
        assert chunk.page_raw_text == "full page text"
        assert chunk.page_type == "text"
        assert chunk.source_format == ".pdf"
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
            page_type="text",
            source_format=".pdf",
            ingestion_timestamp=now,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            chunk.chunk_index = 1  # type: ignore[misc]


class TestSitemapOptions:
    def test_construction_defaults(self) -> None:
        opts = SitemapOptions()
        assert opts.include is None
        assert opts.exclude is None
        assert opts.limit is None
        assert opts.workers == 4
        assert opts.delay == 0.0
        assert opts.timeout == 30.0

    def test_construction_explicit(self) -> None:
        opts = SitemapOptions(
            include="docs/*",
            exclude="drafts/*",
            limit=100,
            workers=8,
            delay=0.5,
            timeout=60.0,
        )
        assert opts.include == "docs/*"
        assert opts.exclude == "drafts/*"
        assert opts.limit == 100
        assert opts.workers == 8
        assert opts.delay == 0.5
        assert opts.timeout == 60.0

    def test_frozen(self) -> None:
        opts = SitemapOptions()
        with pytest.raises(dataclasses.FrozenInstanceError):
            opts.workers = 2  # type: ignore[misc]


class TestChunkConfig:
    def test_defaults(self):
        cfg = ChunkConfig()
        assert cfg.agent_handle is None
        assert cfg.memory_type is None
        assert cfg.summary is None
        assert cfg.max_chars == 2000
        assert cfg.overlap_chars == 200
        assert cfg.collection == "default"
        assert cfg.source_format == ""

    def test_explicit_values(self):
        cfg = ChunkConfig(
            agent_handle="rmh",
            memory_type="fact",
            summary="test summary",
            max_chars=4000,
            overlap_chars=400,
            collection="memory-rmh",
            source_format=".md",
        )
        assert cfg.agent_handle == "rmh"
        assert cfg.memory_type == "fact"
        assert cfg.summary == "test summary"
        assert cfg.max_chars == 4000
        assert cfg.overlap_chars == 400
        assert cfg.collection == "memory-rmh"
        assert cfg.source_format == ".md"

    def test_frozen(self):
        cfg = ChunkConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.max_chars = 500  # type: ignore[misc]

    def test_partial_override(self):
        cfg = ChunkConfig(collection="docs", agent_handle="kpz")
        assert cfg.collection == "docs"
        assert cfg.agent_handle == "kpz"
        assert cfg.max_chars == 2000  # default preserved
