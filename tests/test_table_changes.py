"""The per-database record of writes since the last successful finalize."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import numpy as np

from quarry.config import Settings
from quarry.db import Database
from quarry.db.document_ref import DocumentRef
from quarry.db.lance_connection import LanceConnection
from quarry.db.table_changes import TableChanges
from quarry.ingestion.pipeline import plan_file_chunks
from quarry.page_record import PageRecord

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
