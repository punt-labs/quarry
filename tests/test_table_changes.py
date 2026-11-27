"""The per-database record of writes since the last successful finalize."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import numpy as np
import pytest

from quarry.config import Settings
from quarry.db import Database
from quarry.db.document_ref import DocumentRef
from quarry.db.lance_connection import LanceConnection
from quarry.db.orphan_pages import OrphanPageSweep
from quarry.db.recycling_table import RecyclingTable
from quarry.db.table_changes import TableChanges
from quarry.ingestion.pipeline import plan_file_chunks
from quarry.page_record import PageRecord
from quarry.types import LanceDB, LanceTable

if TYPE_CHECKING:
    from pathlib import Path


def test_databases_at_one_path_share_one_change_record(tmp_path: Path) -> None:
    path = tmp_path / "lancedb"
    first, second = Database.connect(path).db, Database.connect(path).db
    assert isinstance(first, LanceConnection)
    assert isinstance(second, LanceConnection)
    assert first.changes is second.changes
    assert first.changes is TableChanges.for_path(path)


class TestTableChanges:
    def test_never_finalized_needs_a_rewrite(self) -> None:
        assert TableChanges().needs_rewrite()

    def test_clean_after_marking_the_snapshot(self) -> None:
        changes = TableChanges()
        changes.mark_clean(changes.snapshot())
        assert not changes.needs_rewrite()

    def test_write_after_clean_needs_a_rewrite(self) -> None:
        changes = TableChanges()
        changes.mark_clean(changes.snapshot())
        changes.note_write()
        assert changes.needs_rewrite()

    def test_write_after_snapshot_survives_mark_clean(self) -> None:
        changes = TableChanges()
        snapshot = changes.snapshot()
        changes.note_write()
        changes.mark_clean(snapshot)
        assert changes.needs_rewrite()

    def test_stale_snapshot_never_moves_the_mark_backwards(self) -> None:
        changes = TableChanges()
        old = changes.snapshot()
        changes.note_write()
        changes.mark_clean(changes.snapshot())
        changes.mark_clean(old)
        assert not changes.needs_rewrite()

    def test_maintenance_is_due_once_per_interval(self) -> None:
        now = [0.0]
        changes = TableChanges(clock=lambda: now[0])
        assert changes.maintenance_due()
        changes.mark_maintained()
        assert not changes.maintenance_due()
        now[0] += TableChanges.MAINTENANCE_INTERVAL_S
        assert changes.maintenance_due()

    def test_concurrent_writers_and_a_finalizer_never_lose_a_write(self) -> None:
        changes = TableChanges()

        def write_many() -> None:
            for _ in range(500):
                changes.note_write()

        writers = [threading.Thread(target=write_many) for _ in range(4)]
        stop = threading.Event()

        def finalize_forever() -> None:
            while not stop.is_set():
                changes.mark_clean(changes.snapshot())

        finalizer = threading.Thread(target=finalize_forever)
        finalizer.start()
        for t in writers:
            t.start()
        for t in writers:
            t.join()
        stop.set()
        finalizer.join()
        changes.note_write()
        assert changes.needs_rewrite()


class TestWriteFunnel:
    """Every write through a database's tables moves its record."""

    @staticmethod
    def _clean_database(tmp_path: Path) -> tuple[Database, TableChanges, Settings]:
        path = tmp_path / "lancedb"
        database = Database.connect(path)
        settings = Settings(lancedb_path=path, registry_path=tmp_path / "registry.db")
        changes = TableChanges.for_path(path)
        return database, changes, settings

    @staticmethod
    def _insert(
        database: Database, settings: Settings, tmp_path: Path, name: str
    ) -> None:
        doc = tmp_path / name
        doc.write_text("indexable body " * 20)
        chunks, _ = plan_file_chunks(doc, settings, collection="c", document_name=name)
        database.store.insert(chunks, np.zeros((len(chunks), 768), dtype=np.float32))

    def test_chunk_insert_that_creates_the_table(self, tmp_path: Path) -> None:
        database, changes, settings = self._clean_database(tmp_path)
        changes.mark_clean(changes.snapshot())
        self._insert(database, settings, tmp_path, "a.md")
        assert changes.needs_rewrite()

    def test_chunk_insert_into_an_existing_table(self, tmp_path: Path) -> None:
        database, changes, settings = self._clean_database(tmp_path)
        self._insert(database, settings, tmp_path, "a.md")
        changes.mark_clean(changes.snapshot())
        self._insert(database, settings, tmp_path, "b.md")
        assert changes.needs_rewrite()

    def test_document_delete(self, tmp_path: Path) -> None:
        database, changes, settings = self._clean_database(tmp_path)
        self._insert(database, settings, tmp_path, "a.md")
        changes.mark_clean(changes.snapshot())
        database.store.delete_document("a.md", "c")
        assert changes.needs_rewrite()

    def test_collection_purge(self, tmp_path: Path) -> None:
        database, changes, settings = self._clean_database(tmp_path)
        self._insert(database, settings, tmp_path, "a.md")
        changes.mark_clean(changes.snapshot())
        database.store.delete_collection("c")
        assert changes.needs_rewrite()

    def test_delete_tail(self, tmp_path: Path) -> None:
        database, changes, settings = self._clean_database(tmp_path)
        self._insert(database, settings, tmp_path, "a.md")
        changes.mark_clean(changes.snapshot())
        database.store.delete_document_tail(DocumentRef.tail("a.md", "c", 1))
        assert changes.needs_rewrite()

    def test_page_write(self, tmp_path: Path) -> None:
        database, changes, settings = self._clean_database(tmp_path)
        self._insert(database, settings, tmp_path, "a.md")
        changes.mark_clean(changes.snapshot())
        database.store.pages.put([PageRecord("c", "a.md", 99, "late page")])
        assert changes.needs_rewrite()

    def test_read_leaves_the_record_clean(self, tmp_path: Path) -> None:
        database, changes, settings = self._clean_database(tmp_path)
        self._insert(database, settings, tmp_path, "a.md")
        changes.mark_clean(changes.snapshot())
        database.store.count()
        assert not changes.needs_rewrite()

    def test_maintenance_calls_do_not_count_as_writes(self, tmp_path: Path) -> None:
        database, changes, settings = self._clean_database(tmp_path)
        self._insert(database, settings, tmp_path, "a.md")
        changes.mark_clean(changes.snapshot())
        database.optimizer.optimize(force=True)
        assert not changes.needs_rewrite()


_NOT_WRITES = {
    "search",
    "count_rows",
    "stats",
    "optimize",
    "create_index",
    "schema",
    "uri",
}
_SAMPLE_ARGS: dict[str, tuple[object, ...]] = {
    "add": ([],),
    "delete": ("x = 1",),
    "add_columns": ({},),
    "drop_columns": ([],),
}


class _FakeTable:
    """Implement every ``LanceTable`` method by doing nothing."""

    uri = "fake"
    schema = None

    def add(self, data: list[dict[str, object]]) -> None: ...

    def delete(self, predicate: str) -> None: ...

    def add_columns(self, transforms: dict[str, str]) -> None: ...

    def drop_columns(self, columns: list[str]) -> None: ...


class _Owner:
    def __init__(self) -> None:
        self.changes = TableChanges()

    def note_index_rebuild(self) -> None: ...


class TestEveryWriteMethodIsRecorded:
    """A new ``LanceTable`` method must either record itself or be named a non-write."""

    @staticmethod
    def _protocol_methods() -> list[str]:
        return sorted(
            name
            for name, member in vars(LanceTable).items()
            if not name.startswith("_") and callable(member)
        )

    def test_every_method_not_in_the_allow_list_bumps_the_record(self) -> None:
        for name in self._protocol_methods():
            if name in _NOT_WRITES:
                continue
            assert name in _SAMPLE_ARGS, f"{name}: add sample args or allow-list it"
            owner = _Owner()
            table = RecyclingTable(cast("LanceTable", _FakeTable()), owner)
            before = owner.changes.snapshot()
            getattr(table, name)(*_SAMPLE_ARGS[name])
            assert owner.changes.snapshot() > before, f"{name} skips the write record"

    def test_the_allow_list_names_only_real_protocol_members(self) -> None:
        members = set(vars(LanceTable))
        assert members >= _NOT_WRITES

    def test_a_failing_write_still_bumps_the_record(self) -> None:
        class Failing(_FakeTable):
            def delete(self, predicate: str) -> None:
                raise RuntimeError(predicate)

        owner = _Owner()
        table = RecyclingTable(cast("LanceTable", Failing()), owner)
        before = owner.changes.snapshot()
        with pytest.raises(RuntimeError):
            table.delete("x")
        assert owner.changes.snapshot() > before


def test_an_alias_of_the_database_path_shares_its_record(tmp_path: Path) -> None:
    real = tmp_path / "real" / "lancedb"
    real.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real.parent)
    assert TableChanges.for_path(alias / "lancedb") is TableChanges.for_path(real)


def test_an_orphan_sweep_that_deletes_pages_dirties_the_record(tmp_path: Path) -> None:
    database = Database.connect(tmp_path / "lancedb")
    changes = TableChanges.for_path(tmp_path / "lancedb")
    database.store.pages.put([PageRecord("c", "orphan.md", 1, "orphan page")])
    changes.mark_clean(changes.snapshot())
    later = datetime.now(UTC) + timedelta(days=2)
    assert OrphanPageSweep(database.db).run(now=later) == 1
    assert changes.needs_rewrite()


_CONNECTION_NOT_WRITES = {"list_tables", "open_table"}
_CONNECTION_SAMPLE_ARGS: dict[str, tuple[tuple[object, ...], dict[str, object]]] = {
    "create_table": (("t",), {"data": [], "schema": None}),
}


class _FakeTables:
    tables: tuple[str, ...] = ()


class _FakeConnection:
    """Implement every ``LanceDB`` method by doing nothing."""

    def list_tables(self) -> _FakeTables:
        return _FakeTables()

    def open_table(self, name: str) -> _FakeTable:
        return _FakeTable()

    def create_table(
        self, name: str, *, data: list[dict[str, object]], schema: object
    ) -> _FakeTable:
        return _FakeTable()


class TestEveryConnectionWriteIsRecorded:
    """A new ``LanceDB`` method must record itself or be named a non-write."""

    def test_every_method_not_in_the_allow_list_bumps_the_record(self) -> None:
        methods = sorted(
            name
            for name, member in vars(LanceDB).items()
            if not name.startswith("_") and callable(member)
        )
        for name in methods:
            if name in _CONNECTION_NOT_WRITES:
                continue
            assert name in _CONNECTION_SAMPLE_ARGS, f"{name}: add sample args"
            args, kwargs = _CONNECTION_SAMPLE_ARGS[name]
            changes = TableChanges()
            conn = LanceConnection(lambda: cast("LanceDB", _FakeConnection()), changes)
            before = changes.snapshot()
            getattr(conn, name)(*args, **kwargs)
            assert changes.snapshot() > before, f"{name} skips the write record"

    def test_the_allow_list_names_only_real_protocol_members(self) -> None:
        assert set(vars(LanceDB)) >= _CONNECTION_NOT_WRITES
