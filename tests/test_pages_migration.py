"""PagesMigration backfills the pages table, verifies it, then drops page_raw_text."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pyarrow as pa
import pytest

from quarry.db.adaptive_scan import AdaptiveScan
from quarry.db.chunk_store import ChunkStore
from quarry.db.page_store import PageStore
from quarry.db.pages_migration import MigrationOutcome, PagesMigration
from quarry.db.schema import TABLE_NAME, SchemaManager
from quarry.page_record import PageRecord

if TYPE_CHECKING:
    from quarry.types import LanceDB

_DIM = 768
_LEGACY_COLUMN = "page_raw_text"


def _legacy_schema() -> pa.Schema:
    fields = list(SchemaManager.schema())
    at = next(i for i, f in enumerate(fields) if f.name == "chunk_index") + 1
    fields.insert(at, pa.field(_LEGACY_COLUMN, pa.utf8()))
    return pa.schema(fields)


def _row(
    doc: str, page: int, index: int, raw: str, collection: str = "col"
) -> dict[str, object]:
    return {
        "text": f"chunk {index}",
        "vector": [0.0] * _DIM,
        "document_name": doc,
        "document_path": f"/d/{doc}",
        "collection": collection,
        "page_number": page,
        "total_pages": 3,
        "chunk_index": index,
        _LEGACY_COLUMN: raw,
        "page_type": "text",
        "source_format": ".txt",
        "ingestion_timestamp": datetime.now(UTC),
        "agent_handle": "",
        "memory_type": "",
        "summary": "",
        "source_version": "",
    }


def _seed_legacy(db: LanceDB, rows: list[dict[str, object]] | None = None) -> None:
    rows = rows or [
        _row("a.txt", 1, 0, "page one of a"),
        _row("a.txt", 1, 1, "page one of a"),
        _row("a.txt", 2, 2, "page two of a"),
        _row("b.txt", 1, 0, "page one of b"),
    ]
    db.create_table(TABLE_NAME, data=rows, schema=_legacy_schema())


def _columns(db: LanceDB) -> set[str]:
    return {f.name for f in db.open_table(TABLE_NAME).schema}


class TestMigration:
    def test_every_page_lands_in_the_pages_table_with_equal_text(
        self, lance_db: LanceDB
    ) -> None:
        _seed_legacy(lance_db)
        PagesMigration(lance_db).run()
        pages = PageStore(lance_db)
        assert pages.count() == 3
        assert pages.get_page_text("a.txt", 2, "col") == "page two of a"
        assert pages.get_page_text("b.txt", 1, "col") == "page one of b"

    def test_the_legacy_column_is_dropped(self, lance_db: LanceDB) -> None:
        _seed_legacy(lance_db)
        assert _LEGACY_COLUMN in _columns(lance_db)
        outcome = PagesMigration(lance_db).run()
        assert outcome is MigrationOutcome.MIGRATED
        assert _LEGACY_COLUMN not in _columns(lance_db)

    def test_chunks_survive_the_migration(self, lance_db: LanceDB) -> None:
        _seed_legacy(lance_db)
        PagesMigration(lance_db).run()
        assert ChunkStore(lance_db).count() == 4

    def test_a_second_run_does_nothing(self, lance_db: LanceDB) -> None:
        _seed_legacy(lance_db)
        PagesMigration(lance_db).run()
        assert PagesMigration(lance_db).run() is MigrationOutcome.NOT_NEEDED

    def test_no_chunks_table_needs_no_migration(self, lance_db: LanceDB) -> None:
        assert PagesMigration(lance_db).run() is MigrationOutcome.NOT_NEEDED

    def test_a_table_created_by_this_version_needs_none(
        self, lance_db: LanceDB
    ) -> None:
        from tests.test_chunk_store_pages import _chunk, _insert

        _insert(ChunkStore(lance_db), [_chunk(1, 0)])
        assert PagesMigration(lance_db).run() is MigrationOutcome.NOT_NEEDED

    def test_an_interrupted_backfill_is_finished_by_the_next_run(
        self, lance_db: LanceDB
    ) -> None:
        _seed_legacy(lance_db)
        PageStore(lance_db).put([_page("a.txt", 1, "page one of a")])
        assert PagesMigration(lance_db).run() is MigrationOutcome.MIGRATED
        assert PageStore(lance_db).count() == 3


def _page(doc: str, number: int, text: str):
    from quarry.page_record import PageRecord

    return PageRecord("col", doc, number, text)


class TestVerificationRefusesToDrop:
    def test_a_missing_page_keeps_the_column(self, lance_db: LanceDB) -> None:
        _seed_legacy(lance_db)
        real_put = PageStore.put

        def lossy_put(self: PageStore, pages: list[PageRecord]) -> int:
            return real_put(self, [p for p in pages if p.document_name != "b.txt"])

        with patch.object(PageStore, "put", lossy_put):
            outcome = PagesMigration(lance_db).run()
        assert outcome is MigrationOutcome.INCOMPLETE
        assert _LEGACY_COLUMN in _columns(lance_db)

    def test_an_unequal_page_keeps_the_column(self, lance_db: LanceDB) -> None:
        _seed_legacy(lance_db)
        real_put = PageStore.put

        def corrupting_put(self: PageStore, pages: list[PageRecord]) -> int:
            return real_put(
                self, [_page(p.document_name, p.page_number, "x") for p in pages]
            )

        with patch.object(PageStore, "put", corrupting_put):
            outcome = PagesMigration(lance_db).run()
        assert outcome is MigrationOutcome.INCOMPLETE
        assert _LEGACY_COLUMN in _columns(lance_db)

    def test_an_over_budget_page_keeps_the_column_but_moves_the_rest(
        self, lance_db: LanceDB
    ) -> None:
        rows = [_row("big.txt", 1, 0, "a" * (2**20)), _row("a.txt", 1, 0, "fine")]
        _seed_legacy(lance_db, rows)
        outcome = PagesMigration(lance_db).run()
        assert outcome is MigrationOutcome.INCOMPLETE
        assert _LEGACY_COLUMN in _columns(lance_db)
        assert PageStore(lance_db).get_page_text("a.txt", 1, "col") == "fine"


class TestAdaptiveScan:
    def test_a_failing_batch_size_is_halved_until_it_reads(
        self, lance_db: LanceDB
    ) -> None:
        _seed_legacy(lance_db)
        table = lance_db.open_table(TABLE_NAME)
        sizes: list[int] = []
        real = AdaptiveScan._read

        def picky(
            self: AdaptiveScan, offset: int, size: int
        ) -> list[dict[str, object]]:
            sizes.append(size)
            if size > 2:
                raise RuntimeError("Offset overflow error")
            return real(self, offset, size)

        with patch.object(AdaptiveScan, "_read", picky):
            rows = [
                r for batch in AdaptiveScan(table, ["text"], start=8) for r in batch
            ]
        assert len(rows) == 4
        assert sizes[:3] == [8, 4, 2]

    def test_a_single_row_that_cannot_be_read_raises(self, lance_db: LanceDB) -> None:
        _seed_legacy(lance_db)
        table = lance_db.open_table(TABLE_NAME)

        def never(
            self: AdaptiveScan, offset: int, size: int
        ) -> list[dict[str, object]]:
            raise RuntimeError("Offset overflow error")

        with (
            patch.object(AdaptiveScan, "_read", never),
            pytest.raises(RuntimeError, match="overflow"),
        ):
            list(AdaptiveScan(table, ["text"], start=4))
