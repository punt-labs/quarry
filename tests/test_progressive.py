"""Unit tests for ProgressiveIndexer: bounded flush, ordering, checkpoints."""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import pytest

from quarry.db.chunk_store import ChunkStore
from quarry.db.chunk_table import ChunkTable
from quarry.ingestion.progressive import FlushCheckpoint, ProgressiveIndexer
from quarry.models import Chunk

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from quarry.types import LanceDB

_DIM = 4  # 16 bytes per vector — small, exact byte accounting.
_VEC_BYTES = _DIM * 4


def _chunk(index: int) -> Chunk:
    return Chunk(
        document_name="doc",
        document_path="/doc",
        collection="col",
        page_number=1,
        total_pages=1,
        chunk_index=index,
        text=f"chunk {index}",
        page_raw_text="raw",
        page_type="text",
        source_format=".txt",
        ingestion_timestamp=datetime.now(UTC),
    )


def _window(start: int, count: int) -> tuple[list[Chunk], NDArray[np.float32]]:
    chunks = [_chunk(start + i) for i in range(count)]
    vectors = np.zeros((count, _DIM), dtype=np.float32)
    return chunks, vectors


class _FakeTarget:
    """Records inserts, checkpoints, and their interleaving for assertions."""

    def __init__(self) -> None:
        self.inserted: list[list[dict[str, object]]] = []
        self.flushes: list[list[FlushCheckpoint]] = []
        self.order: list[str] = []

    def build_records(
        self, chunks: list[Chunk], vectors: NDArray[np.float32]
    ) -> list[dict[str, object]]:
        return [
            {"chunk_index": c.chunk_index, "vector": v.tolist()}
            for c, v in zip(chunks, vectors, strict=True)
        ]

    def insert_records(self, records: list[dict[str, object]]) -> int:
        self.inserted.append(records)
        self.order.append("insert")
        return len(records)

    def on_flush(self, checkpoints: Sequence[FlushCheckpoint]) -> None:
        self.flushes.append(list(checkpoints))
        self.order.append("flush")


def test_flush_bytes_must_be_positive() -> None:
    with pytest.raises(ValueError, match="flush_bytes must be positive"):
        ProgressiveIndexer(_FakeTarget(), flush_bytes=0)


def test_off_by_one_total_equals_flush_boundary() -> None:
    """total_vector_bytes == flush_bytes fires exactly one flush, no residual."""
    target = _FakeTarget()
    indexer = ProgressiveIndexer(target, flush_bytes=2 * _VEC_BYTES)
    indexer.begin_file("f", total_chunks=2)
    indexer.add_window("f", *_window(0, 2))  # buffer == flush_bytes → flush
    assert len(target.inserted) == 1
    indexer.complete_file("f")
    indexer.drain()
    # No second Lance add on drain (nothing residual); one insert total.
    assert len(target.inserted) == 1
    assert sum(len(r) for r in target.inserted) == 2


def test_single_window_larger_than_budget_flushes_atomically() -> None:
    """A window whose bytes exceed the budget still flushes as one unit."""
    target = _FakeTarget()
    indexer = ProgressiveIndexer(target, flush_bytes=2 * _VEC_BYTES)
    indexer.begin_file("f", total_chunks=10)
    indexer.add_window("f", *_window(0, 10))  # one window > budget
    assert len(target.inserted) == 1
    assert len(target.inserted[0]) == 10
    assert indexer.peak_buffer_bytes == 10 * _VEC_BYTES


def test_mid_file_flush_before_completion() -> None:
    """A large document flushes several times before it completes."""
    target = _FakeTarget()
    indexer = ProgressiveIndexer(target, flush_bytes=2 * _VEC_BYTES)
    indexer.begin_file("f", total_chunks=6)
    for start in range(0, 6, 2):
        indexer.add_window("f", *_window(start, 2))
    # 3 size-flushes happened before complete_file was ever called.
    assert len(target.inserted) == 3
    indexer.complete_file("f")
    indexer.drain()
    assert indexer.inserted_count == 6


@pytest.mark.parametrize("total", [10, 100, 1000])
def test_bounded_peak_flat_across_total(total: int) -> None:
    """Peak buffer is bounded by flush_bytes + one window, independent of total."""
    target = _FakeTarget()
    flush_bytes = 4 * _VEC_BYTES
    indexer = ProgressiveIndexer(target, flush_bytes=flush_bytes)
    indexer.begin_file("f", total_chunks=total)
    for start in range(0, total, 2):
        indexer.add_window("f", *_window(start, 2))
    indexer.complete_file("f")
    indexer.drain()
    assert indexer.inserted_count == total
    # Peak never exceeds the budget plus a single window (2 vectors here).
    assert indexer.peak_buffer_bytes <= flush_bytes + 2 * _VEC_BYTES


def test_lance_add_precedes_registry_commit() -> None:
    """Every flush inserts to Lance before checkpointing the registry (G4)."""
    target = _FakeTarget()
    indexer = ProgressiveIndexer(target, flush_bytes=2 * _VEC_BYTES)
    indexer.begin_file("f", total_chunks=2)
    indexer.add_window("f", *_window(0, 2))
    indexer.complete_file("f")
    indexer.drain()
    assert target.order[0] == "insert"
    assert target.order[1] == "flush"


def test_completion_checkpoint_emitted_once() -> None:
    """A file's completion is reported exactly once, not re-emitted on drain."""
    target = _FakeTarget()
    indexer = ProgressiveIndexer(target, flush_bytes=2 * _VEC_BYTES)
    indexer.begin_file("f", total_chunks=2)
    indexer.add_window("f", *_window(0, 2))  # size-flush reaches total → complete
    indexer.complete_file("f")
    indexer.drain()
    completes = [cp for flush in target.flushes for cp in flush if cp.file_id == "f"]
    assert sum(1 for cp in completes if cp.complete) == 1


def test_zero_chunk_file_still_completes() -> None:
    """A file that produced no chunks gets a single completion checkpoint."""
    target = _FakeTarget()
    indexer = ProgressiveIndexer(target, flush_bytes=2 * _VEC_BYTES)
    indexer.begin_file("empty", total_chunks=0)
    indexer.complete_file("empty")
    indexer.drain()
    assert target.inserted == []  # no Lance write
    checkpoints = [cp for flush in target.flushes for cp in flush]
    assert len(checkpoints) == 1
    assert checkpoints[0].chunks_committed == 0
    assert checkpoints[0].complete is True


def test_resume_watermark_advances_from_seed() -> None:
    """On resume, watermark starts at the seed and advances by chunks added."""
    target = _FakeTarget()
    indexer = ProgressiveIndexer(target, flush_bytes=100 * _VEC_BYTES)
    indexer.begin_file("f", resume_watermark=5, total_chunks=8)
    indexer.add_window("f", *_window(5, 3))  # add [5, 8)
    indexer.complete_file("f")
    indexer.drain()
    checkpoint = target.flushes[-1][-1]
    assert checkpoint.chunks_committed == 8
    assert checkpoint.complete is True


def test_g4_flush_carries_final_and_partial_atomically() -> None:
    """One flush reports A complete and B mid-file in a single on_flush call."""
    target = _FakeTarget()
    # Budget large enough that A's final + B's window share one flush at drain.
    indexer = ProgressiveIndexer(target, flush_bytes=100 * _VEC_BYTES)
    indexer.begin_file("A", total_chunks=2)
    indexer.begin_file("B", total_chunks=4)
    indexer.add_window("A", *_window(0, 2))  # A complete
    indexer.complete_file("A")
    indexer.add_window("B", *_window(0, 2))  # B partial (2 of 4)
    indexer.drain()
    # Exactly one on_flush carried both files' checkpoints.
    assert len(target.flushes) == 1
    by_id = {cp.file_id: cp for cp in target.flushes[0]}
    assert by_id["A"].complete is True
    assert by_id["A"].chunks_committed == 2
    assert by_id["B"].complete is False
    assert by_id["B"].chunks_committed == 2


# ---------------------------------------------------------------------------
# Progressive visibility against a real LanceDB (MVCC blue/green for free)
# ---------------------------------------------------------------------------

_REAL_DIM = 768


def _real_window(start: int, count: int) -> tuple[list[Chunk], NDArray[np.float32]]:
    chunks = [_chunk(start + i) for i in range(count)]
    return chunks, np.zeros((count, _REAL_DIM), dtype=np.float32)


class _StoreTarget:
    """FlushTarget backed by a real ChunkStore, with an optional per-flush delay."""

    def __init__(self, store: ChunkStore, delay: float = 0.0) -> None:
        self._store = store
        self._delay = delay

    def build_records(
        self, chunks: list[Chunk], vectors: NDArray[np.float32]
    ) -> list[dict[str, object]]:
        return ChunkTable.build_records(chunks, vectors)

    def insert_records(self, records: list[dict[str, object]]) -> int:
        result = self._store.insert_records(records)
        if self._delay:
            time.sleep(self._delay)
        return result

    def on_flush(self, checkpoints: Sequence[FlushCheckpoint]) -> None:
        """No registry in this visibility harness."""


class TestProgressiveVisibility:
    def test_each_flush_is_immediately_visible(self, lance_db: LanceDB) -> None:
        """A fresh reader sees a monotonically growing count as flushes commit."""
        store = ChunkStore(lance_db)
        indexer = ProgressiveIndexer(_StoreTarget(store), flush_bytes=2 * _REAL_DIM * 4)
        indexer.begin_file("f", total_chunks=6)
        seen: list[int] = []
        for start in range(0, 6, 2):
            indexer.add_window("f", *_real_window(start, 2))  # 2 vectors → flush
            seen.append(ChunkStore(lance_db).count())  # fresh reader each time
        assert seen == sorted(seen)  # monotonic non-decreasing
        assert seen[-1] == 6

    def test_concurrent_reader_never_blocks(self, lance_db: LanceDB) -> None:
        """A reader thread querying during flushes sees monotonic counts, no error."""
        store = ChunkStore(lance_db)
        indexer = ProgressiveIndexer(
            _StoreTarget(store, delay=0.01), flush_bytes=2 * _REAL_DIM * 4
        )
        indexer.begin_file("f", total_chunks=20)
        counts: list[int] = []
        errors: list[Exception] = []
        done = threading.Event()

        def reader() -> None:
            while not done.is_set():
                try:
                    counts.append(ChunkStore(lance_db).count())
                except Exception as exc:  # noqa: BLE001 - test asserts none occur
                    errors.append(exc)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for start in range(0, 20, 2):
                indexer.add_window("f", *_real_window(start, 2))
            indexer.complete_file("f")
            indexer.drain()
        finally:
            done.set()
            thread.join()

        assert errors == []  # reader never blocked or raised
        assert counts == sorted(counts)  # monotonic non-decreasing
        assert ChunkStore(lance_db).count() == 20
