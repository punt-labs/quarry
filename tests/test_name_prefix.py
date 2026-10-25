"""Tests for quarry.db.name_prefix: the names under a prefix, one row per document."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from quarry.db import Database
from quarry.db.name_prefix import NamePrefix
from quarry.models import Chunk
from tests.fakes import FakeEmbeddingBackend, patched_embedder

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_COLLECTION = "c"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    path = tmp_path / "lancedb"
    path.mkdir()
    with patched_embedder(FakeEmbeddingBackend(768)):
        yield Database.connect(path)


def _store(
    db: Database,
    name: str,
    *,
    chunks: int = 1,
    collection: str = _COLLECTION,
    url_sourced: bool = True,
) -> None:
    """Write *chunks* chunks under *name*, numbered from zero as the funnel does.

    A page fetched from a URL records the URL as its path; a document that was
    only labelled with one records none.
    """
    rows = [
        Chunk(
            document_name=name,
            document_path=name if url_sourced else "",
            collection=collection,
            page_number=1,
            total_pages=1,
            chunk_index=index,
            text=f"body {index}",
            page_raw_text=f"body {index}",
            page_type="text",
            source_format="inline",
            ingestion_timestamp=datetime.now(tz=UTC),
        )
        for index in range(chunks)
    ]
    vectors = FakeEmbeddingBackend(768).embed_texts([c.text for c in rows])
    db.store.insert(rows, vectors)


def _names(db: Database, prefix: str, collection: str = _COLLECTION) -> list[str]:
    return NamePrefix(db.db).names(prefix, collection)


class TestNames:
    def test_it_lists_the_names_that_start_with_the_prefix(self, db: Database) -> None:
        _store(db, "https://h.test/a")
        _store(db, "https://h.test/b")
        _store(db, "https://other.test/a")
        assert _names(db, "https://h.test") == ["https://h.test/a", "https://h.test/b"]

    def test_a_document_with_many_chunks_is_listed_once(self, db: Database) -> None:
        _store(db, "https://h.test/a", chunks=5)
        assert _names(db, "https://h.test") == ["https://h.test/a"]

    def test_another_collection_is_not_listed(self, db: Database) -> None:
        _store(db, "https://h.test/a", collection="elsewhere")
        assert _names(db, "https://h.test") == []

    def test_an_empty_store_lists_nothing(self, db: Database) -> None:
        assert _names(db, "https://h.test") == []

    def test_a_document_only_labelled_with_a_url_is_not_listed(
        self, db: Database
    ) -> None:
        _store(db, "https://h.test/a", url_sourced=False)
        _store(db, "https://h.test/b")
        assert _names(db, "https://h.test") == ["https://h.test/b"]

    def test_an_upper_case_source_scheme_is_still_a_page(self, db: Database) -> None:
        rows = [
            Chunk(
                document_name="https://h.test/a",
                document_path="HTTPS://h.test/a",
                collection=_COLLECTION,
                page_number=1,
                total_pages=1,
                chunk_index=0,
                text="body",
                page_raw_text="body",
                page_type="text",
                source_format="inline",
                ingestion_timestamp=datetime.now(tz=UTC),
            )
        ]
        db.store.insert(rows, FakeEmbeddingBackend(768).embed_texts(["body"]))
        assert _names(db, "https://h.test") == ["https://h.test/a"]

    def test_the_match_ignores_case(self, db: Database) -> None:
        _store(db, "https://Example.com/x")
        assert _names(db, "https://example.com") == ["https://Example.com/x"]
        assert _names(db, "https://EXAMPLE.COM") == ["https://Example.com/x"]


class TestThePrefixIsLiteral:
    @pytest.mark.parametrize(
        ("prefix", "stored", "other"),
        [
            ("https://h_st", "https://h_st/a", "https://hxst/a"),
            ("https://h%st", "https://h%st/a", "https://hostile.test/a"),
            ("https://h\\st", "https://h\\st/a", "https://hst/a"),
            ("https://h'st", "https://h'st/a", "https://hst/a"),
        ],
        ids=["underscore", "percent", "backslash", "quote"],
    )
    def test_a_metacharacter_matches_only_itself(
        self, db: Database, prefix: str, stored: str, other: str
    ) -> None:
        _store(db, stored)
        _store(db, other)
        assert _names(db, prefix) == [stored]


class TestTheScanLimit:
    def test_a_scan_that_reaches_the_limit_warns(
        self,
        db: Database,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr("quarry.db.name_prefix._SCAN_LIMIT", 2)
        for n in range(3):
            _store(db, f"https://h.test/{n}")
        with caplog.at_level(logging.WARNING, logger="quarry.db.name_prefix"):
            found = _names(db, "https://h.test")
        assert len(found) == 2
        assert "limit" in caplog.text

    def test_one_row_per_document_keeps_a_chunky_store_under_the_limit(
        self,
        db: Database,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr("quarry.db.name_prefix._SCAN_LIMIT", 5)
        for n in range(3):
            _store(db, f"https://h.test/{n}", chunks=4)
        with caplog.at_level(logging.WARNING, logger="quarry.db.name_prefix"):
            found = _names(db, "https://h.test")
        assert len(found) == 3
        assert [r for r in caplog.records if r.name == "quarry.db.name_prefix"] == []
