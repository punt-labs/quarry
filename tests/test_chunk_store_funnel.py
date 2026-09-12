"""Tests for quarry.ingestion.chunk_store_funnel — the DES-036 convergence point.

These exercise ``_chunk_embed_store`` directly, closing the coverage gap where
it was previously only reached transitively through ``ingest_document`` /
``ingest_content`` in ``test_pipeline.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from quarry.config import Settings
from quarry.db import Database
from quarry.ingestion.chunk_store_funnel import _chunk_embed_store
from quarry.ingestion.extracted_document import ExtractedDocument
from quarry.ingestion.ingest_context import IngestContext, Progress
from quarry.ingestion.ingest_stats import IngestStats
from quarry.models import Chunk, PageContent, PageType

if TYPE_CHECKING:
    import pytest

_PROGRESS = Progress(None)


def _settings() -> Settings:
    return Settings()


def _page() -> PageContent:
    return PageContent(
        document_name="doc.txt",
        document_path="/doc.txt",
        page_number=1,
        total_pages=1,
        text="hello",
        page_type=PageType.TEXT,
    )


def _extracted(*, stats: IngestStats | None = None) -> ExtractedDocument:
    return ExtractedDocument(
        pages=[_page()], stats=stats or IngestStats(), source_format=".txt"
    )


def _chunk() -> Chunk:
    return Chunk(
        document_name="doc.txt",
        document_path="/doc.txt",
        collection="default",
        page_number=1,
        total_pages=1,
        chunk_index=0,
        text="hello",
        page_raw_text="hello",
        page_type="text",
        source_format=".txt",
        ingestion_timestamp=datetime.now(tz=UTC),
    )


def _context(*, overwrite: bool = False) -> IngestContext:
    return IngestContext(Database(MagicMock()), _settings(), overwrite=overwrite)


class TestEmptyChunksFailClosed:
    def test_zero_chunks_skips_delete_and_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )
        deleted: list[str] = []
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.delete_document",
            lambda _self, name, **_kw: deleted.append(name),
        )
        inserted: list[list[dict[str, object]]] = []

        def _record_insert(_self: object, records: list[dict[str, object]]) -> int:
            inserted.append(records)
            return len(records)

        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert_records", _record_insert
        )

        result = _chunk_embed_store(
            _extracted(), "doc.txt", _PROGRESS, _context(overwrite=True)
        )

        assert deleted == []
        assert inserted == []
        assert result["chunks"] == 0


class TestNonEmptyChunksStore:
    def test_overwrite_deletes_before_storing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [_chunk()],
        )
        calls: list[str] = []

        def _record_insert(_self: object, records: list[dict[str, object]]) -> int:
            calls.append("insert")
            return len(records)

        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert_records", _record_insert
        )
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.delete_document",
            lambda _self, name, **_kw: calls.append(f"delete:{name}"),
        )

        result = _chunk_embed_store(
            _extracted(), "doc.txt", _PROGRESS, _context(overwrite=True)
        )

        assert calls == ["delete:doc.txt", "insert"]
        assert result["chunks"] == 1

    def test_no_overwrite_skips_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [_chunk()],
        )
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert_records",
            lambda _self, records: len(records),
        )
        deleted: list[str] = []
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.delete_document",
            lambda _self, name, **_kw: deleted.append(name),
        )

        result = _chunk_embed_store(_extracted(), "doc.txt", _PROGRESS, _context())

        assert deleted == []
        assert result["chunks"] == 1


class TestStatsMergedIntoResult:
    def test_stats_fields_appear_in_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )

        stats = IngestStats(total_pages=1, text_pages=1, image_pages=0)
        result = _chunk_embed_store(
            _extracted(stats=stats), "doc.txt", _PROGRESS, _context()
        )

        assert result.get("total_pages") == 1
        assert result.get("text_pages") == 1
        assert result.get("image_pages") == 0
        assert result["document_name"] == "doc.txt"
        assert result["collection"] == "default"


class TestProgressReporting:
    def test_progress_called_for_each_phase(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [_chunk()],
        )
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert_records",
            lambda _self, records: len(records),
        )
        messages: list[str] = []
        progress = Progress(messages.append, log=False)

        _chunk_embed_store(_extracted(), "doc.txt", progress, _context())

        assert "Chunking" in messages
        assert any(m.startswith("Created") for m in messages)
        assert any(m.startswith("Done") for m in messages)


class TestDocumentNameRedaction:
    """A plain (unscrubbed) URL ingest's document_name IS the URL, so the
    "Done: ..." success line and the zero-chunks warning must never leak a
    userinfo/query secret from it (CWE-532). document_name itself -- what's
    actually deleted/stored/returned -- is unaffected either way.
    """

    def test_url_document_name_redacts_secrets_on_success(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [_chunk()],
        )
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert_records",
            lambda _self, records: len(records),
        )
        credentialed_name = "https://user:pass@example.com/page?token=abc123"
        messages: list[str] = []

        # The "Done: ..." line is emitted via Progress.__call__'s own
        # logger.info -- under quarry.ingestion.ingest_context, NOT this
        # module's logger -- so caplog must be raised at that logger or its
        # capture stays empty and the assertions below would pass vacuously.
        with caplog.at_level("INFO", logger="quarry.ingestion.ingest_context"):
            result = _chunk_embed_store(
                _extracted(), credentialed_name, Progress(messages.append), _context()
            )

        # document_name itself is untouched -- identity/storage is unaffected.
        assert result["document_name"] == credentialed_name
        done = [m for m in messages if m.startswith("Done:")]
        assert done == ["Done: 1 chunks indexed from https://example.com/page"]
        assert "user:pass" not in caplog.text
        assert "token=abc123" not in caplog.text
        # Exact match, not a host substring check (CodeQL
        # py/incomplete-url-substring-sanitization): pins the whole log
        # record rather than a fragment that could also match an unrelated
        # look-alike string.
        info_records = [r.getMessage() for r in caplog.records]
        assert "Done: 1 chunks indexed from https://example.com/page" in info_records

    def test_url_document_name_redacts_secrets_on_zero_chunks(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [],
        )
        credentialed_name = "https://user:pass@example.com/page?token=abc123"

        with caplog.at_level("WARNING"):
            _chunk_embed_store(_extracted(), credentialed_name, _PROGRESS, _context())

        assert "user:pass" not in caplog.text
        assert "token=abc123" not in caplog.text
        # Exact match, not a host substring check (CodeQL
        # py/incomplete-url-substring-sanitization): pins the whole log
        # record rather than a fragment that could also match an unrelated
        # look-alike string.
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert warnings == [
            "pipeline: https://example.com/page produced zero chunks — "
            "keeping any prior document, storing nothing"
        ]

    def test_plain_document_name_is_unaffected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-URL document_name (inline content's caller-supplied name)
        must round-trip unchanged through the same redaction call.
        """
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [_chunk()],
        )
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert_records",
            lambda _self, records: len(records),
        )
        messages: list[str] = []

        _chunk_embed_store(
            _extracted(), "notes.md", Progress(messages.append), _context()
        )

        done = [m for m in messages if m.startswith("Done:")]
        assert done == ["Done: 1 chunks indexed from notes.md"]

    def test_non_url_name_with_url_syntax_chars_is_not_truncated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """'#' and '?' are valid characters in a plain document name (a
        ``remember`` title, a filename) but are urlsplit's fragment/query
        delimiters -- CaptureUrl.redacted silently drops everything from the
        first one onward. The http(s):// prefix guard must keep a non-URL
        name whole, not hand it to CaptureUrl at all.
        """
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [_chunk()],
        )
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert_records",
            lambda _self, records: len(records),
        )
        name = "notes #3 (draft)?.md"
        messages: list[str] = []

        _chunk_embed_store(_extracted(), name, Progress(messages.append), _context())

        done = [m for m in messages if m.startswith("Done:")]
        assert done == [f"Done: 1 chunks indexed from {name}"]

    def test_name_that_would_make_captureurl_raise_appears_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A string resembling a broken URL authority makes ``urlsplit``
        raise ``ValueError`` from inside ``CaptureUrl.redacted`` -- after
        chunks are already stored, so the guard's http(s):// prefix check
        (which excludes this name; it has no scheme at all) and the
        try/except fallback inside ``_display_name`` must both hold: this
        must never raise, and the display value is the name unchanged.
        """
        monkeypatch.setattr(
            "quarry.ingestion.streaming.chunk_pages",
            lambda _pages, max_chars, overlap_chars, **_kw: [_chunk()],
        )
        monkeypatch.setattr(
            "quarry.db.chunk_store.ChunkStore.insert_records",
            lambda _self, records: len(records),
        )
        name = "a://[::1"
        messages: list[str] = []

        _chunk_embed_store(_extracted(), name, Progress(messages.append), _context())

        done = [m for m in messages if m.startswith("Done:")]
        assert done == [f"Done: 1 chunks indexed from {name}"]
