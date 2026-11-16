"""Regression tests for the pages-table review findings."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import numpy as np
import pytest
from starlette.testclient import TestClient

from quarry.daemon.app import build_app
from quarry.daemon.context import DaemonContext
from quarry.db.chunk_store import ChunkStore
from quarry.db.chunk_writer import ChunkWriter
from quarry.db.orphan_pages import GRACE_PERIOD, OrphanPageSweep
from quarry.db.page_store import PageStore
from quarry.db.pages_migration import LEGACY_COLUMN, MigrationOutcome, PagesMigration
from quarry.db.schema import TABLE_NAME
from quarry.db.storage import get_db
from quarry.http_resources import QuarryResources
from quarry.page_record import PageRecord
from tests.test_chunk_store_pages import _chunk, _insert
from tests.test_http_server import _inject_mocks, _mock_settings
from tests.test_pages_migration import _columns, _row, _seed_legacy

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.types import LanceDB

_BIG = "a" * (2**20)


def _aged() -> datetime:
    return datetime.now(UTC) + GRACE_PERIOD + timedelta(minutes=1)


def _page(doc: str, number: int, text: str) -> PageRecord:
    return PageRecord("col", doc, number, text)


class TestMigrationNullLegacyText:
    """Finding 1: a null legacy value must never overwrite a real page."""

    def test_rerun_after_incomplete_keeps_new_documents_page_text(
        self, lance_db: LanceDB
    ) -> None:
        _seed_legacy(lance_db, [_row("big.txt", 1, 0, _BIG), _row("a.txt", 1, 0, "ok")])
        assert PagesMigration(lance_db).run() is MigrationOutcome.INCOMPLETE
        _insert(ChunkStore(lance_db), [_chunk(1, 0, doc="new.txt")])
        PagesMigration(lance_db).run()
        assert PageStore(lance_db).get_page_text("new.txt", 1, "col") == (
            "full text of page 1"
        )

    def test_a_null_legacy_value_is_not_stored_as_the_string_none(
        self, lance_db: LanceDB
    ) -> None:
        row = _row("n.txt", 1, 0, "x")
        row[LEGACY_COLUMN] = None
        _seed_legacy(lance_db, [row])
        PagesMigration(lance_db).run()
        assert PageStore(lance_db).get_page_text("n.txt", 1, "col") != "None"


class TestMigrationConflicts:
    """Finding 9: differing inline text for one page key must not be dropped."""

    def test_two_different_values_for_one_page_keep_the_column(
        self, lance_db: LanceDB
    ) -> None:
        _seed_legacy(
            lance_db, [_row("a.txt", 1, 0, "version one"), _row("a.txt", 1, 1, "two")]
        )
        assert PagesMigration(lance_db).run() is MigrationOutcome.INCOMPLETE
        assert LEGACY_COLUMN in _columns(lance_db)


class TestMigrationBatching:
    """Finding 7: one write per batch, and finished pages are not rewritten."""

    def test_pages_are_written_in_batches_not_one_by_one(
        self, lance_db: LanceDB
    ) -> None:
        _seed_legacy(lance_db, [_row("d.txt", n, n, f"p{n}") for n in range(1, 41)])
        calls: list[int] = []
        real = PageStore.put

        def counting(self: PageStore, pages: list[PageRecord]) -> int:
            calls.append(len(pages))
            return real(self, pages)

        with patch.object(PageStore, "put", counting):
            PagesMigration(lance_db).run()
        assert len(calls) <= 2

    def test_a_rerun_skips_pages_already_stored_equal(self, lance_db: LanceDB) -> None:
        _seed_legacy(
            lance_db, [_row("big.txt", 1, 0, _BIG), _row("d.txt", 1, 1, "same")]
        )
        PagesMigration(lance_db).run()
        written: list[int] = []
        real = PageStore.put

        def spy(self: PageStore, pages: list[PageRecord]) -> int:
            written.append(len(pages))
            return real(self, pages)

        with patch.object(PageStore, "put", spy):
            PagesMigration(lance_db).run()
        assert sum(written) == 0


class TestOverBudgetLegacyPage:
    """Finding 3: an over-budget legacy page stays readable through /v1/show."""

    def test_show_serves_the_retained_inline_text(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        _seed_legacy(get_db(settings.lancedb_path), [_row("big.txt", 1, 0, _BIG)])
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        with TestClient(build_app(ctx), raise_server_exceptions=False) as tc:
            resp = tc.get("/v1/show?document=big.txt&page=1&collection=col")
        assert resp.status_code == 200
        assert resp.json()["text"] == _BIG


class TestSweepLocking:
    """Finding 4: the sweep deletes through the pages lock."""

    def test_the_sweep_deletes_through_the_page_store(self, lance_db: LanceDB) -> None:
        PageStore(lance_db).put([_page("d.txt", 1, "orphan")])
        seen: list[str] = []
        real = PageStore.delete_where

        def spy(self: PageStore, predicate: str) -> None:
            seen.append(predicate)
            real(self, predicate)

        with patch.object(PageStore, "delete_where", spy):
            OrphanPageSweep(lance_db).run(now=_aged())
        assert len(seen) == 1


class TestSweepCompleteness:
    """Findings 5 and 6: a bounded query must not hide live or orphan pages."""

    def test_a_live_page_beyond_the_chunk_query_limit_survives(
        self, lance_db: LanceDB
    ) -> None:
        PageStore(lance_db).put([_page("d.txt", n, f"p{n}") for n in (1, 2, 3)])
        ChunkStore(lance_db).insert(
            [_chunk(1, 0), _chunk(1, 1), _chunk(2, 2), _chunk(3, 3)],
            np.zeros((4, 768), dtype=np.float32),
        )
        with patch("quarry.db.orphan_pages._BATCH_ROWS", 1):
            swept = OrphanPageSweep(lance_db).run(now=_aged())
        assert swept == 0
        assert PageStore(lance_db).count() == 3

    def test_an_orphan_behind_a_full_batch_of_live_pages_is_swept(
        self, lance_db: LanceDB
    ) -> None:
        ChunkStore(lance_db).insert(
            [_chunk(1, 0), _chunk(2, 1)], np.zeros((2, 768), dtype=np.float32)
        )
        PageStore(lance_db).put([_page("d.txt", 9, "orphan")])
        with patch("quarry.db.orphan_pages._BATCH_ROWS", 2):
            swept = OrphanPageSweep(lance_db).run(now=_aged())
        assert swept == 1
        assert PageStore(lance_db).get_page_text("d.txt", 9, "col") is None
        assert PageStore(lance_db).count() == 2


class TestOrphanPagesAreNotServed:
    """Finding 8: a page with no live chunk is not retrievable."""

    def test_show_hides_a_page_whose_chunks_are_gone(self, tmp_path: Path) -> None:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        _insert(ctx.database.store, [_chunk(1, 0)])
        ctx.database.store._writer._delete_chunks("document_name = 'd.txt'")
        assert PageStore(ctx.database.db).count() == 1
        with TestClient(build_app(ctx), raise_server_exceptions=False) as tc:
            resp = tc.get("/v1/show?document=d.txt&page=1&collection=col")
        assert resp.status_code == 404


class TestDeleteDoesNotStrandChunks:
    """Finding 2: a delete is serialized with a page-and-chunk add."""

    def test_a_delete_between_put_and_chunk_add_cannot_drop_the_page(
        self, lance_db: LanceDB
    ) -> None:
        store = ChunkStore(lance_db)
        _insert(store, [_chunk(1, 0, doc="old.txt")])
        in_put = threading.Event()
        release = threading.Event()
        real_add = ChunkWriter._add_chunks

        def paused_add(self: ChunkWriter, rows: list[dict[str, object]]) -> None:
            in_put.set()
            release.wait(timeout=5)
            real_add(self, rows)

        errors: list[Exception] = []

        def ingest() -> None:
            try:
                with patch.object(ChunkWriter, "_add_chunks", paused_add):
                    _insert(store, [_chunk(1, 1, doc="d.txt")])
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(exc)

        t = threading.Thread(target=ingest)
        t.start()
        assert in_put.wait(timeout=5)
        deleter = threading.Thread(target=lambda: store.delete_document("d.txt", "col"))
        deleter.start()
        release.set()
        t.join(timeout=10)
        deleter.join(timeout=10)
        assert not errors
        table = lance_db.open_table(TABLE_NAME)
        chunk_rows = table.count_rows("document_name = 'd.txt'")
        page = PageStore(lance_db).get_page_text("d.txt", 1, "col")
        assert chunk_rows == 0 or page is not None


class TestMigrationFailureDoesNotStopStartup:
    """Finding 10: a migration failure is logged and startup continues."""

    @pytest.mark.parametrize(
        "exc", [OSError("disk"), RuntimeError("scan"), ValueError("x")]
    )
    def test_warm_survives_a_failing_migration(
        self, tmp_path: Path, exc: Exception
    ) -> None:
        settings = _mock_settings(tmp_path)
        resources = QuarryResources(settings)
        with patch.object(PagesMigration, "run", side_effect=exc):
            resources.warm()

    def test_roster_survives_a_failing_migration(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock

        from quarry.config import Settings
        from quarry.daemon.watch_roster import WatchRoster

        base = Settings(
            quarry_root=tmp_path,
            lancedb_path=tmp_path / "default" / "lancedb",
            registry_path=tmp_path / "default" / "registry.db",
        )
        roster = WatchRoster(
            MagicMock(),
            active_db="default",
            active_database=MagicMock(),
            base_settings=base,
        )
        with patch.object(PagesMigration, "run", side_effect=RuntimeError("scan")):
            roster.ensure_database("sibling")
        assert roster.database_of("sibling") is not None


class TestFailedMigrationLeavesPagesReadable:
    def test_a_page_stays_readable_after_a_failed_startup_migration(
        self, tmp_path: Path
    ) -> None:
        settings = _mock_settings(tmp_path)
        _seed_legacy(
            get_db(settings.lancedb_path), [_row("a.txt", 1, 0, "legacy text")]
        )
        resources = QuarryResources(settings)
        with patch.object(PagesMigration, "run", side_effect=RuntimeError("scan")):
            resources.warm()
        text = resources.database.store.readable.text_of("a.txt", 1, "col")
        assert text == "legacy text"


class TestShowResolvesTheCollectionFromTheLiveChunk:
    """Round 2, finding 1: a live chunk may not authorize another collection's page."""

    @pytest.mark.parametrize("orphan_collection", ["aaa-other", "zzz-other"])
    def test_an_orphan_in_another_collection_is_never_served(
        self, tmp_path: Path, orphan_collection: str
    ) -> None:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        _insert(ctx.database.store, [_chunk(1, 0)])
        orphan = PageRecord(orphan_collection, "d.txt", 1, "ORPHAN TEXT")
        PageStore(ctx.database.db).put([orphan])
        with TestClient(build_app(ctx), raise_server_exceptions=False) as tc:
            resp = tc.get("/v1/show?document=d.txt&page=1")
        assert resp.status_code == 200
        assert resp.json()["text"] == "full text of page 1"

    def test_a_live_chunk_without_its_page_is_not_served_from_an_orphan(
        self, lance_db: LanceDB
    ) -> None:
        from quarry.db.readable_page import ReadablePage

        _insert(ChunkStore(lance_db), [_chunk(1, 0)])
        PageStore(lance_db).delete_where("collection = 'col'")
        PageStore(lance_db).put([PageRecord("other", "d.txt", 1, "ORPHAN TEXT")])
        with pytest.raises(KeyError):
            ReadablePage(lance_db).text_of("d.txt", 1, None)

    def test_an_explicit_collection_still_selects_that_collection(
        self, tmp_path: Path
    ) -> None:
        settings = _mock_settings(tmp_path)
        ctx = DaemonContext(settings)
        _inject_mocks(ctx)
        _insert(ctx.database.store, [_chunk(1, 0)])
        with TestClient(build_app(ctx), raise_server_exceptions=False) as tc:
            resp = tc.get("/v1/show?document=d.txt&page=1&collection=col")
        assert resp.json()["text"] == "full text of page 1"


class TestMigrationMemoryIsBounded:
    """Round 2, finding 2: the seen-page state holds digests, not page text."""

    def test_the_seen_state_holds_no_page_text(self) -> None:
        from quarry.db.pages_migration import _SeenPages

        seen = _SeenPages()
        big = "x" * 1_000_000
        for n in range(20):
            assert seen.is_new(PageRecord("c", "d", n, big + str(n)))
        held = sum(len(v) for v in seen._first.values())
        assert held < 20 * 100

    def test_a_conflict_is_still_detected_by_digest(self) -> None:
        from quarry.db.pages_migration import _SeenPages

        seen = _SeenPages()
        assert seen.is_new(PageRecord("c", "d", 1, "one"))
        assert not seen.is_new(PageRecord("c", "d", 1, "two"))
        assert seen.conflicts(PageRecord("c", "d", 1, "two")) == 1
        assert seen.conflicts(PageRecord("c", "d", 1, "one")) == 0


class TestVerifyReadsEachPageOnce:
    """Round 2, finding 6: one stored-text read per distinct page, not per chunk."""

    def test_a_page_shared_by_many_chunks_is_fetched_once(
        self, lance_db: LanceDB
    ) -> None:
        _seed_legacy(lance_db, [_row("d.txt", 1, i, "shared") for i in range(30)])
        reads: list[int] = []
        real = PageStore.get_page_text

        def counting(
            store: PageStore, name: str, number: int, collection: str | None = None
        ) -> str | None:
            reads.append(number)
            return real(store, name, number, collection)

        with patch.object(PageStore, "get_page_text", counting):
            outcome = PagesMigration(lance_db).run()
        assert outcome is MigrationOutcome.MIGRATED
        assert len(reads) <= 2

    def test_a_conflict_among_the_chunks_of_a_page_still_blocks_the_drop(
        self, lance_db: LanceDB
    ) -> None:
        _seed_legacy(lance_db, [_row("d.txt", 1, 0, "a"), _row("d.txt", 1, 1, "b")])
        assert PagesMigration(lance_db).run() is MigrationOutcome.INCOMPLETE


def _held_by_caller() -> bool:
    """Return whether the calling thread holds ``WRITE_LOCK``.

    A reentrant lock cannot be probed from its owner, so a second thread tries a
    non-blocking acquire: it fails exactly when the caller holds the lock.
    """
    from quarry.db.page_store import WRITE_LOCK

    outcome: list[bool] = []

    def probe() -> None:
        free = WRITE_LOCK.acquire(blocking=False)
        if free:
            WRITE_LOCK.release()
        outcome.append(not free)

    worker = threading.Thread(target=probe)
    worker.start()
    worker.join(timeout=5)
    return outcome[0]


class TestSweepKeepsTheWriteLockShort:
    """Round 2, finding 8: the batch lookup is outside the lock, the recheck inside."""

    def test_the_batch_lookup_runs_without_the_write_lock(
        self, lance_db: LanceDB
    ) -> None:
        _insert(ChunkStore(lance_db), [_chunk(n, n) for n in range(1, 6)])
        owned_during_lookup: list[bool] = []
        real = OrphanPageSweep._batch_live_pages

        def spy(
            sweep: OrphanPageSweep, rows: list[dict[str, object]]
        ) -> set[tuple[str, str, int]]:
            owned_during_lookup.append(_held_by_caller())
            return real(sweep, rows)

        with patch.object(OrphanPageSweep, "_batch_live_pages", spy):
            OrphanPageSweep(lance_db).run(now=_aged())
        assert owned_during_lookup == [False]

    def test_the_delete_recheck_holds_the_write_lock(self, lance_db: LanceDB) -> None:
        PageStore(lance_db).put([_page("d.txt", 1, "orphan")])
        owned_during_recheck: list[bool] = []
        real = OrphanPageSweep._has_chunk

        def spy(sweep: OrphanPageSweep, predicate: str) -> bool:
            owned_during_recheck.append(_held_by_caller())
            return real(sweep, predicate)

        with patch.object(OrphanPageSweep, "_has_chunk", spy):
            OrphanPageSweep(lance_db).run(now=_aged())
        assert owned_during_recheck == [True]

    def test_a_live_page_is_never_deleted_by_the_batch_lookup(
        self, lance_db: LanceDB
    ) -> None:
        _insert(ChunkStore(lance_db), [_chunk(1, 0), _chunk(2, 1)])
        PageStore(lance_db).put([_page("d.txt", 9, "orphan")])
        assert OrphanPageSweep(lance_db).run(now=_aged()) == 1
        assert PageStore(lance_db).count() == 2

    def test_a_chunk_that_arrives_after_the_batch_lookup_saves_its_page(
        self, lance_db: LanceDB
    ) -> None:
        PageStore(lance_db).put([_page("d.txt", 1, "late")])
        real = OrphanPageSweep._batch_live_pages

        def late_writer(
            sweep: OrphanPageSweep, rows: list[dict[str, object]]
        ) -> set[tuple[str, str, int]]:
            found = real(sweep, rows)
            _insert(ChunkStore(lance_db), [_chunk(1, 0)])
            return found

        with patch.object(OrphanPageSweep, "_batch_live_pages", late_writer):
            assert OrphanPageSweep(lance_db).run(now=_aged()) == 0
        assert PageStore(lance_db).get_page_text("d.txt", 1, "col") is not None
