"""Bounded progressive commit: buffer embed windows and flush to LanceDB.

The ``ProgressiveIndexer`` replaces sync's accumulate-all-then-commit-once with a
size-gated, mid-file-capable flush (DES-034). It buffers ``(chunks, vectors)``
windows and, when the buffered vector bytes reach ``flush_bytes``, writes them to
LanceDB in one add and asks its ``FlushTarget`` to commit one atomic registry
transaction covering every file the flush touched. Peak resident vectors are
bounded by ``flush_bytes + one window``, independent of file or collection size.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np
    from numpy.typing import NDArray

    from quarry.models import Chunk


@dataclass(frozen=True, slots=True)
class FlushCheckpoint:
    """One file's committed watermark after a flush.

    ``chunks_committed`` is the contiguous count durable in LanceDB and reflected
    in the registry; ``complete`` is True once the file's final chunk is durable
    (the registry then clears the partial-hash mark).
    """

    file_id: str
    chunks_committed: int
    complete: bool


class FlushTarget(Protocol):
    """Sink for a progressive flush: build rows, write them, checkpoint them.

    ``build_records`` and ``insert_records`` are the LanceDB write half;
    ``on_flush`` is the registry half and MUST commit every checkpoint in one
    transaction after the single Lance add (DES-034 §2.2 / G4).
    """

    def build_records(
        self, chunks: list[Chunk], vectors: NDArray[np.float32]
    ) -> list[dict[str, object]]: ...

    def insert_records(self, records: list[dict[str, object]]) -> int: ...

    def on_flush(self, checkpoints: Sequence[FlushCheckpoint]) -> None: ...


class ProgressiveIndexer:
    """Buffer embed windows and flush to LanceDB when the byte budget is hit.

    Single responsibility: own the record buffer, the size-flush policy, and the
    per-file watermark bookkeeping that produces one atomic registry checkpoint
    per flush. Writes are performed by exactly one caller (the sync consumer
    thread or the single-doc path), so no lock is needed here — serialization is
    the caller's invariant (DES-034 §2).
    """

    __slots__ = (
        "_buffer_bytes",
        "_buffered",
        "_completed",
        "_dirty",
        "_flush_bytes",
        "_inserted",
        "_peak_bytes",
        "_records",
        "_target",
        "_total",
        "_watermark",
    )

    _target: FlushTarget
    _flush_bytes: int
    _records: list[dict[str, object]]
    _buffer_bytes: int
    _peak_bytes: int
    _inserted: int
    _watermark: dict[str, int]
    _total: dict[str, int]
    _buffered: dict[str, int]
    _dirty: set[str]
    _completed: set[str]

    def __new__(cls, target: FlushTarget, *, flush_bytes: int) -> Self:
        if flush_bytes <= 0:
            msg = f"flush_bytes must be positive, got {flush_bytes}"
            raise ValueError(msg)
        self = super().__new__(cls)
        self._target = target
        self._flush_bytes = flush_bytes
        self._records = []
        self._buffer_bytes = 0
        self._peak_bytes = 0
        self._inserted = 0
        self._watermark = {}
        self._total = {}
        self._buffered = {}
        self._dirty = set()
        self._completed = set()
        return self

    @property
    def inserted_count(self) -> int:
        """Total records written to LanceDB across all flushes so far."""
        return self._inserted

    @property
    def peak_buffer_bytes(self) -> int:
        """High-water mark of buffered vector bytes — bounded by N + one window."""
        return self._peak_bytes

    def begin_file(
        self,
        file_id: str,
        *,
        resume_watermark: int = 0,
        total_chunks: int,
    ) -> None:
        """Register a file, seeding its watermark at ``resume_watermark``.

        For a fresh file ``resume_watermark`` is 0; for within-file resume it is
        the count of chunks already durable and reflected in the registry. Must
        be called before any ``add_window``/``complete_file`` for *file_id*.
        """
        self._watermark[file_id] = resume_watermark
        self._total[file_id] = total_chunks
        self._buffered[file_id] = 0

    def add_window(
        self,
        file_id: str,
        chunks: list[Chunk],
        vectors: NDArray[np.float32],
    ) -> None:
        """Buffer one embed window; flush if the byte budget is reached.

        The whole window is buffered before the threshold check, so a single
        window larger than the budget still flushes atomically (never split).
        """
        records = self._target.build_records(chunks, vectors)
        self._records.extend(records)
        self._buffer_bytes += int(vectors.nbytes)
        self._buffered[file_id] += len(chunks)
        self._dirty.add(file_id)
        if self._buffer_bytes > self._peak_bytes:
            self._peak_bytes = self._buffer_bytes
        if self._buffer_bytes >= self._flush_bytes:
            self._flush()

    def complete_file(self, file_id: str) -> None:
        """Mark a file's producer exhausted so its completion row is written.

        Needed even when the file's last window already flushed (or the file
        produced zero chunks): the drain then emits the completion checkpoint.
        """
        self._dirty.add(file_id)

    def drain(self) -> None:
        """Flush any residual buffer and emit remaining checkpoints."""
        self._flush()

    def _flush(self) -> None:
        """Write buffered records (one Lance add), then checkpoint atomically.

        Ordering is Lance-add first, registry second (DES-034 §2.2): a crash
        between them leaves durable chunks with an unadvanced watermark, which
        within-file resume reconciles via delete-tail — never a duplicate.
        """
        if self._records:
            self._inserted += self._target.insert_records(self._records)
            self._records = []
        self._buffer_bytes = 0

        checkpoints = self._collect_checkpoints()
        self._dirty = set()
        if checkpoints:
            self._target.on_flush(checkpoints)

    def _collect_checkpoints(self) -> list[FlushCheckpoint]:
        """Advance watermarks for dirty files and build their checkpoints."""
        checkpoints: list[FlushCheckpoint] = []
        for file_id in self._dirty:
            advanced = self._buffered.get(file_id, 0)
            self._watermark[file_id] += advanced
            self._buffered[file_id] = 0
            watermark = self._watermark[file_id]
            complete = watermark >= self._total[file_id]
            already_done = file_id in self._completed
            if advanced == 0 and (not complete or already_done):
                continue
            if complete:
                self._completed.add(file_id)
            checkpoints.append(
                FlushCheckpoint(
                    file_id=file_id,
                    chunks_committed=watermark,
                    complete=complete,
                )
            )
        return checkpoints
