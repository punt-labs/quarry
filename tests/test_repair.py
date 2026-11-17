"""TableRepair rebuilds a chunks table optimize() can no longer compact."""

from __future__ import annotations

import fcntl
import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import lancedb
import pytest

from quarry.daemon_guard import DaemonGuard
from quarry.db.adaptive_scan import AdaptiveScan
from quarry.db.orphan_pages import OrphanPageSweep
from quarry.db.page_store import PageStore
from quarry.db.readable_page import ReadablePage
from quarry.db.schema import TABLE_NAME, SchemaManager
from quarry.db.storage import get_db
from quarry.page_record import PageRecord
from quarry.repair import TableRepair
from quarry.repair_plan import RepairPlan
from quarry.run_dir import RunDir

if TYPE_CHECKING:
    from quarry.types import LanceDB, LanceTable

_DIM = 768
_OVER_BUDGET = "x" * (2**20 + 16)
_BINARY = "�" * 64 + "binary"


def _row(
    doc: str, index: int, text: str, value: float = 0.25, collection: str = "col"
) -> dict[str, object]:
    return {
        "text": text,
        "vector": [value + index / 1000] * _DIM,
        "document_name": doc,
        "document_path": f"/d/{doc}",
        "collection": collection,
        "page_number": 1,
        "total_pages": 1,
        "chunk_index": index,
        "page_type": "text",
        "source_format": ".txt",
        "ingestion_timestamp": datetime.now(UTC),
        "agent_handle": "",
        "memory_type": "",
        "summary": "",
        "source_version": "",
    }


def _seed(path: Path, rows: list[dict[str, object]]) -> LanceDB:
    db = get_db(path)
    table = db.create_table(TABLE_NAME, data=[], schema=SchemaManager.schema())
    for row in rows:
        table.add([row])  # one version per add, like the daemon's flushes
    return db


def _leak(path: Path, megabytes: int) -> None:
    data = path / f"{TABLE_NAME}.lance" / "data"
    data.mkdir(exist_ok=True)
    (data / "leaked.lance").write_bytes(b"\0" * megabytes * 2**20)


def _tree(path: Path) -> dict[str, str]:
    return {
        str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


def _size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


class _Locked:
    """A TableRepair whose plan() and rebuild() each run inside a held session."""

    def __init__(self, path: Path) -> None:
        self._repair = TableRepair(path, path.parent)

    def plan(self) -> RepairPlan:
        with self._repair.session() as session:
            return session.plan()

    def rebuild(self) -> int:
        with self._repair.session() as session:
            return session.rebuild()


def _repair(path: Path) -> _Locked:
    return _Locked(path)


def _mixed(path: Path) -> LanceDB:
    rows = [_row("good.txt", i, f"good chunk {i}") for i in range(5)]
    rows += [_row("pdf.bin", i, _BINARY) for i in range(2)]
    rows += [_row("huge.txt", 0, _OVER_BUDGET)]
    return _seed(path, rows)


class TestScanCostIsLinear:
    """Reads at a deep offset cost no more than at the start (no re-skipping)."""

    def test_a_read_near_the_end_costs_about_the_same_as_one_at_the_start(
        self, tmp_path: Path
    ) -> None:
        import time

        db = get_db(tmp_path / "lancedb")
        table = db.create_table(TABLE_NAME, data=[], schema=SchemaManager.schema())
        for i in range(20):
            table.add([_row(f"d{i}", j, "t" * 500) for j in range(500)])
        cols = list(SchemaManager.schema().names)

        def best_of(offset: int) -> float:
            times = []
            for _ in range(5):
                start = time.perf_counter()
                table.search().select(cols).limit(256).offset(offset).to_list()
                times.append(time.perf_counter() - start)
            return min(times)

        # A re-skipping scan would make the deep read ~40x the first one.
        assert best_of(9_700) < 10 * max(best_of(0), 0.002)


class TestPlan:
    def test_counts_binary_over_budget_and_kept_rows(self, tmp_path: Path) -> None:
        _mixed(tmp_path / "lancedb")
        plan = _repair(tmp_path / "lancedb").plan()
        assert (plan.rows, plan.binary_rows, plan.over_budget_rows) == (8, 2, 1)
        assert plan.kept_rows == 5

    def test_reports_on_disk_size_larger_than_live_size_with_leaks(
        self, tmp_path: Path
    ) -> None:
        _mixed(tmp_path / "lancedb")
        _leak(tmp_path / "lancedb", 6)
        plan = _repair(tmp_path / "lancedb").plan()
        assert plan.on_disk_bytes > 6 * 2**20 > plan.live_bytes

    def test_changes_nothing_on_disk(self, tmp_path: Path) -> None:
        _mixed(tmp_path / "lancedb")
        before = _tree(tmp_path / "lancedb")
        _repair(tmp_path / "lancedb").plan()
        assert _tree(tmp_path / "lancedb") == before

    def test_absent_table_is_an_empty_plan(self, tmp_path: Path) -> None:
        get_db(tmp_path / "lancedb")
        assert _repair(tmp_path / "lancedb").plan().rows == 0

    def test_reads_in_batches_of_at_most_256_rows(self, tmp_path: Path) -> None:
        _mixed(tmp_path / "lancedb")
        starts: list[int] = []
        real = AdaptiveScan

        def spy(table: LanceTable, columns: list[str], *, start: int) -> AdaptiveScan:
            starts.append(start)
            return real(table, columns, start=start)

        with patch("quarry.repair.AdaptiveScan", spy):
            _repair(tmp_path / "lancedb").plan()
        assert starts
        assert max(starts) <= 256


class TestChunkBudget:
    """A chunk is held to the chunk budget (64 KiB), not the page budget (1 MiB)."""

    def _seed_mid_sized(self, path: Path) -> None:
        mid = "x" * 100_000  # over a chunk's limit, under a page's
        _seed(path, [_row("ok.txt", 0, "fine"), _row("mid.txt", 0, mid)])

    def test_plan_counts_a_chunk_over_the_chunk_budget(self, tmp_path: Path) -> None:
        self._seed_mid_sized(tmp_path / "lancedb")
        assert _repair(tmp_path / "lancedb").plan().over_budget_rows == 1

    def test_rebuild_drops_a_chunk_over_the_chunk_budget(self, tmp_path: Path) -> None:
        self._seed_mid_sized(tmp_path / "lancedb")
        _repair(tmp_path / "lancedb").rebuild()
        rows = get_db(tmp_path / "lancedb").open_table(TABLE_NAME).search().to_list()
        assert [r["document_name"] for r in rows] == ["ok.txt"]


class TestDocumentLevelBinaryVerdict:
    """A document is binary by the bar ingest used, not by one odd chunk."""

    def _doc(
        self, name: str, odd: str, odd_at: int, chunks: int = 40
    ) -> list[dict[str, object]]:
        return [
            _row(name, i, odd if i == odd_at else "clean prose " * 150)
            for i in range(chunks)
        ]

    def test_one_dense_chunk_among_clean_ones_keeps_the_document(
        self, tmp_path: Path
    ) -> None:
        dense = "\ufffd" * 120 + " math"
        _seed(tmp_path / "lancedb", self._doc("math.txt", dense, odd_at=7))
        plan = _repair(tmp_path / "lancedb").plan()
        assert (plan.binary_rows, plan.kept_rows) == (0, 40)

    def test_a_later_chunk_that_opens_with_replacement_characters_is_kept(
        self, tmp_path: Path
    ) -> None:
        opens_bad = "\ufffd\ufffd\ufffd table of symbols"
        _seed(tmp_path / "lancedb", self._doc("tbl.txt", opens_bad, odd_at=5))
        assert _repair(tmp_path / "lancedb").plan().binary_rows == 0

    def test_a_dense_opening_chunk_of_an_otherwise_clean_document_is_kept(
        self, tmp_path: Path
    ) -> None:
        dense_head = "sym " + "\ufffd" * 300 + " symbols"
        _seed(tmp_path / "lancedb", self._doc("sym.txt", dense_head, odd_at=0))
        assert _repair(tmp_path / "lancedb").plan().binary_rows == 0

    def test_a_document_that_opens_with_a_binary_signature_is_dropped(
        self, tmp_path: Path
    ) -> None:
        _seed(
            tmp_path / "lancedb",
            self._doc("img.png", "\x89PNG then bytes", odd_at=0),
        )
        assert _repair(tmp_path / "lancedb").plan().binary_rows == 40

    def test_a_document_that_is_mostly_non_text_is_dropped(
        self, tmp_path: Path
    ) -> None:
        rows = [_row("bin.dat", i, "\ufffd" * 900 + "abc" * 30) for i in range(10)]
        _seed(tmp_path / "lancedb", rows)
        assert _repair(tmp_path / "lancedb").plan().binary_rows == 10


class TestRebuild:
    def test_drops_bad_rows_and_keeps_good_rows_with_their_vectors(
        self, tmp_path: Path
    ) -> None:
        _mixed(tmp_path / "lancedb")
        _repair(tmp_path / "lancedb").rebuild()
        rows = (
            get_db(tmp_path / "lancedb")
            .open_table(TABLE_NAME)
            .search()
            .limit(100)
            .to_list()
        )
        assert sorted(str(r["text"]) for r in rows) == [
            f"good chunk {i}" for i in range(5)
        ]
        by_text = {str(r["text"]): r["vector"] for r in rows}
        assert by_text["good chunk 3"] == pytest.approx([0.25 + 3 / 1000] * _DIM)

    def test_recreates_the_collection_and_text_indexes(self, tmp_path: Path) -> None:
        _mixed(tmp_path / "lancedb")
        _repair(tmp_path / "lancedb").rebuild()
        table = lancedb.connect(str(tmp_path / "lancedb")).open_table(TABLE_NAME)
        assert {i.name for i in table.list_indices()} == {"collection_idx", "text_idx"}

    def test_full_text_search_works_after_the_rebuild(self, tmp_path: Path) -> None:
        _mixed(tmp_path / "lancedb")
        _repair(tmp_path / "lancedb").rebuild()
        table = lancedb.connect(str(tmp_path / "lancedb")).open_table(TABLE_NAME)
        assert len(table.search("chunk", query_type="fts").limit(10).to_list()) == 5

    def test_on_disk_size_collapses_when_files_were_leaked(
        self, tmp_path: Path
    ) -> None:
        _mixed(tmp_path / "lancedb")
        _leak(tmp_path / "lancedb", 8)
        before = _size(tmp_path / "lancedb")
        after = _repair(tmp_path / "lancedb").rebuild()
        assert before > 8 * 2**20
        assert after < 2**20
        assert _size(tmp_path / "lancedb") == after

    def test_leaves_no_scratch_or_backup_directory_behind(self, tmp_path: Path) -> None:
        _mixed(tmp_path / "lancedb")
        _repair(tmp_path / "lancedb").rebuild()
        assert [p.name for p in (tmp_path / "lancedb").iterdir()] == [
            f"{TABLE_NAME}.lance"
        ]

    def test_a_table_with_no_bad_rows_keeps_every_row(self, tmp_path: Path) -> None:
        _seed(tmp_path / "lancedb", [_row("a", i, f"t{i}") for i in range(3)])
        _repair(tmp_path / "lancedb").rebuild()
        table = get_db(tmp_path / "lancedb").open_table(TABLE_NAME)
        assert table.count_rows() == 3

    def test_more_rows_than_one_batch_are_all_kept(self, tmp_path: Path) -> None:
        db = get_db(tmp_path / "lancedb")
        table = db.create_table(TABLE_NAME, data=[], schema=SchemaManager.schema())
        table.add([_row("big", i, f"t{i}") for i in range(600)])
        _repair(tmp_path / "lancedb").rebuild()
        assert get_db(tmp_path / "lancedb").open_table(TABLE_NAME).count_rows() == 600

    def test_a_failure_mid_rebuild_leaves_the_original_byte_for_byte(
        self, tmp_path: Path
    ) -> None:
        _mixed(tmp_path / "lancedb")
        _leak(tmp_path / "lancedb", 1)
        before = _tree(tmp_path / "lancedb")
        with (
            patch.object(TableRepair, "_create_indexes", side_effect=OSError("boom")),
            pytest.raises(OSError, match="boom"),
        ):
            _repair(tmp_path / "lancedb").rebuild()
        assert _tree(tmp_path / "lancedb") == before

    def test_a_failure_leaves_no_temporary_sibling(self, tmp_path: Path) -> None:
        _mixed(tmp_path / "lancedb")
        before = sorted(p.name for p in (tmp_path / "lancedb").iterdir())
        with (
            patch.object(TableRepair, "_create_indexes", side_effect=OSError("boom")),
            pytest.raises(OSError, match="boom"),
        ):
            _repair(tmp_path / "lancedb").rebuild()
        assert sorted(p.name for p in (tmp_path / "lancedb").iterdir()) == before

    def test_a_kept_row_missing_its_vector_aborts_before_the_swap(
        self, tmp_path: Path
    ) -> None:
        _mixed(tmp_path / "lancedb")
        before = _tree(tmp_path / "lancedb")
        with (
            patch.object(TableRepair, "_vector_is_whole", return_value=False),
            pytest.raises(ValueError, match="vector"),
        ):
            _repair(tmp_path / "lancedb").rebuild()
        assert _tree(tmp_path / "lancedb") == before

    def test_a_scratch_directory_stranded_by_a_dead_run_is_removed(
        self, tmp_path: Path
    ) -> None:
        _mixed(tmp_path / "lancedb")
        stranded = tmp_path / ".repair-99999"
        stranded.mkdir()
        (stranded / "junk").write_bytes(b"x")
        _repair(tmp_path / "lancedb").rebuild()
        assert not stranded.exists()

    def test_absent_table_is_refused(self, tmp_path: Path) -> None:
        get_db(tmp_path / "lancedb")
        with pytest.raises(ValueError, match="no chunks table"):
            _repair(tmp_path / "lancedb").rebuild()


class TestPagesOfDroppedDocuments:
    """Pages of a dropped document stay as orphans for OrphanPageSweep."""

    def _seed_pages(self, db: LanceDB) -> None:
        PageStore(db).put(
            [
                PageRecord("col", "good.txt", 1, "good page"),
                PageRecord("col", "pdf.bin", 1, "binary page"),
            ]
        )

    def test_repair_leaves_every_page_row_in_place(self, tmp_path: Path) -> None:
        self._seed_pages(_mixed(tmp_path / "lancedb"))
        _repair(tmp_path / "lancedb").rebuild()
        assert PageStore(get_db(tmp_path / "lancedb")).count() == 2

    def test_a_dropped_documents_page_is_not_readable(self, tmp_path: Path) -> None:
        self._seed_pages(_mixed(tmp_path / "lancedb"))
        _repair(tmp_path / "lancedb").rebuild()
        pages = ReadablePage(get_db(tmp_path / "lancedb"))
        assert pages.text_of("good.txt", 1, "col") == "good page"
        with pytest.raises(KeyError):
            pages.text_of("pdf.bin", 1, "col")

    def test_the_orphan_sweep_removes_only_the_dropped_documents_page(
        self, tmp_path: Path
    ) -> None:
        self._seed_pages(_mixed(tmp_path / "lancedb"))
        _repair(tmp_path / "lancedb").rebuild()
        db = get_db(tmp_path / "lancedb")
        swept = OrphanPageSweep(db).run(now=datetime.now(UTC) + timedelta(hours=25))
        assert swept == 1
        assert PageStore(db).get_page_text("good.txt", 1, "col") == "good page"


class TestDaemonGuard:
    def _guard(self, tmp_path: Path) -> DaemonGuard:
        return DaemonGuard((RunDir(tmp_path),))

    def test_no_port_file_and_no_lock_means_stopped(self, tmp_path: Path) -> None:
        assert not self._guard(tmp_path).is_running()

    def test_a_stale_port_file_with_nothing_listening_means_stopped(
        self, tmp_path: Path
    ) -> None:
        RunDir(tmp_path).port_file.write(65000)
        with patch("quarry.daemon_guard.HealthProbe.fetch", return_value=None):
            assert not self._guard(tmp_path).is_running()

    def test_a_port_that_answers_health_means_running(self, tmp_path: Path) -> None:
        RunDir(tmp_path).port_file.write(65000)
        with patch(
            "quarry.daemon_guard.HealthProbe.fetch", return_value={"state": "ready"}
        ):
            assert self._guard(tmp_path).is_running()

    def test_a_held_run_dir_lock_means_running_without_a_port_file(
        self, tmp_path: Path
    ) -> None:
        fd = os.open(str(RunDir(tmp_path).lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert self._guard(tmp_path).is_running()
        finally:
            os.close(fd)

    def test_exclusive_blocks_a_daemon_from_starting_meanwhile(
        self, tmp_path: Path
    ) -> None:
        guard = self._guard(tmp_path)
        with guard.exclusive():
            assert self._guard(tmp_path).is_running()
        assert not self._guard(tmp_path).is_running()

    def test_exclusive_refuses_when_a_live_port_answers_though_no_lock_is_held(
        self, tmp_path: Path
    ) -> None:
        RunDir(tmp_path).port_file.write(65000)
        with (
            patch(
                "quarry.daemon_guard.HealthProbe.fetch", return_value={"state": "ready"}
            ),
            pytest.raises(ValueError, match="daemon"),
            self._guard(tmp_path).exclusive(),
        ):
            pass

    def test_exclusive_refuses_before_taking_any_lock_when_a_port_answers(
        self, tmp_path: Path
    ) -> None:
        RunDir(tmp_path).port_file.write(65000)
        with (
            patch(
                "quarry.daemon_guard.HealthProbe.fetch", return_value={"state": "ready"}
            ),
            pytest.raises(ValueError, match="daemon"),
            self._guard(tmp_path).exclusive(),
        ):
            pass
        assert not RunDir(tmp_path).lock_path.exists()

    def test_exclusive_refuses_when_a_daemon_holds_the_lock(
        self, tmp_path: Path
    ) -> None:
        fd = os.open(str(RunDir(tmp_path).lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (
                pytest.raises(ValueError, match="daemon"),
                self._guard(tmp_path).exclusive(),
            ):
                pass
        finally:
            os.close(fd)


def _hold(run_dir: Path) -> int:
    run_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(RunDir(run_dir).lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


class TestSiblingDatabases:
    """A daemon started for one database writes to every sibling in its roster."""

    def _roster(self, tmp_path: Path) -> tuple[Path, Path]:
        root = tmp_path / "data"
        for name in ("default", "work"):
            (root / name).mkdir(parents=True)
            (root / name / "registry.db").write_bytes(b"")
        _mixed(root / "work" / "lancedb")
        return root, root / "work" / "lancedb"

    def test_a_daemon_on_another_database_blocks_the_guard(
        self, tmp_path: Path
    ) -> None:
        root, work = self._roster(tmp_path)
        fd = _hold(root / "default")
        try:
            assert DaemonGuard.roster(work.parent, root).is_running()
        finally:
            os.close(fd)

    def test_no_daemon_anywhere_means_stopped(self, tmp_path: Path) -> None:
        root, work = self._roster(tmp_path)
        assert not DaemonGuard.roster(work.parent, root).is_running()

    def test_a_directory_without_a_registry_is_not_a_roster_member(
        self, tmp_path: Path
    ) -> None:
        root, work = self._roster(tmp_path)
        (root / "scratch").mkdir()
        fd = _hold(root / "scratch")
        try:
            assert not DaemonGuard.roster(work.parent, root).is_running()
        finally:
            os.close(fd)

    def test_repair_of_one_database_refuses_while_a_sibling_has_a_daemon(
        self, tmp_path: Path
    ) -> None:
        root, work = self._roster(tmp_path)
        before = _tree(work)
        fd = _hold(root / "default")
        try:
            with pytest.raises(ValueError, match="daemon"):
                TableRepair(work, root).session().__enter__()
        finally:
            os.close(fd)
        assert _tree(work) == before

    def test_the_session_keeps_every_sibling_locked_until_it_ends(
        self, tmp_path: Path
    ) -> None:
        root, work = self._roster(tmp_path)
        with TableRepair(work, root).session():
            assert DaemonGuard.roster(work.parent, root).is_running()
        assert not DaemonGuard.roster(work.parent, root).is_running()


class TestPlanAndRebuildShareOneLockedState:
    def test_a_write_between_plan_and_rebuild_cannot_happen_under_the_lock(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "data"
        (root / "default").mkdir(parents=True)
        (root / "default" / "registry.db").write_bytes(b"")
        db = root / "default" / "lancedb"
        _mixed(db)
        with TableRepair(db, root).session() as session:
            plan = session.plan()
            # A would-be daemon cannot take the lock while the session holds it.
            with (
                pytest.raises(ValueError, match="daemon"),
                DaemonGuard.roster(db.parent, root).exclusive(),
            ):
                pass
            session.rebuild()
        kept = get_db(db).open_table(TABLE_NAME).count_rows()
        assert kept == plan.kept_rows

    def test_rebuild_does_not_reacquire_the_lock_it_was_given(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "data"
        (root / "default").mkdir(parents=True)
        (root / "default" / "registry.db").write_bytes(b"")
        db = root / "default" / "lancedb"
        _mixed(db)
        with TableRepair(db, root).session() as session:
            session.rebuild()  # would raise "a quarryd daemon owns" if it re-locked
        assert get_db(db).open_table(TABLE_NAME).count_rows() == 5
