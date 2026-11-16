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
from quarry.db.orphan_pages import GRACE_PERIOD, OrphanPageSweep
from quarry.db.page_store import PageStore
from quarry.db.pages_migration import LEGACY_COLUMN, MigrationOutcome, PagesMigration
from quarry.db.schema import TABLE_NAME
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
        import lancedb

        settings = _mock_settings(tmp_path)
        _seed_legacy(
            lancedb.connect(str(settings.lancedb_path)),  # type: ignore[arg-type]
            [_row("big.txt", 1, 0, _BIG)],
        )
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
        real_add = type(store._writer)._add_chunks

        def paused_add(self: object, rows: list[dict[str, object]]) -> None:
            in_put.set()
            release.wait(timeout=5)
            real_add(self, rows)  # type: ignore[arg-type]

        errors: list[BaseException] = []

        def ingest() -> None:
            try:
                with patch.object(type(store._writer), "_add_chunks", paused_add):
                    _insert(store, [_chunk(1, 1, doc="d.txt")])
            except BaseException as exc:  # noqa: BLE001
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
        import lancedb

        settings = _mock_settings(tmp_path)
        _seed_legacy(
            lancedb.connect(str(settings.lancedb_path)),  # type: ignore[arg-type]
            [_row("a.txt", 1, 0, "legacy text")],
        )
        resources = QuarryResources(settings)
        with patch.object(PagesMigration, "run", side_effect=RuntimeError("scan")):
            resources.warm()
        text = resources.database.store.readable.text_of("a.txt", 1, "col")
        assert text == "legacy text"
