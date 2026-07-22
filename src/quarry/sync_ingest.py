"""Progressive per-collection ingest: producer/consumer over a shared file core."""

from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from queue import Queue
from typing import TYPE_CHECKING, Self

from quarry.db.chunk_table import ChunkTable
from quarry.ingestion.file_indexer import SingleFileIndexer
from quarry.ingestion.progressive import ProgressiveIndexer
from quarry.ingestion.streaming import DocumentStreamer
from quarry.sync_messages import FileMeta, WindowMsg

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import numpy as np
    from numpy.typing import NDArray

    from quarry.config import Settings
    from quarry.db.chunk_store import ChunkStore
    from quarry.ingestion.progressive import FlushCheckpoint
    from quarry.models import Chunk
    from quarry.sync_registry import FileRecord, SyncRegistry

logger = logging.getLogger(__name__)

_RECOVERABLE = (sqlite3.Error, OSError, ValueError, RuntimeError, TimeoutError)


class CollectionIngestor:
    """Run a collection's to-ingest files through bounded progressive commit.

    Doubles as the ProgressiveIndexer's FlushTarget (DES-034 §2.2 / G4). Producers
    plan + embed in bounded windows; the single consumer thread performs every
    LanceDB write (delete and add) and registry write. Producers always emit one
    sentinel and the consumer catches every error, so neither side can dead-lock.

    The per-file steps — plan, reconcile the store, build the checkpoint row —
    are delegated to a composed :class:`SingleFileIndexer`, the same core the
    watch loop's ``FileIndexJob`` uses (DES-045); this class owns only the bulk
    orchestration (producer pool, single consumer, abort/liveness) and the ONE
    cross-file ``ProgressiveIndexer`` that keeps the fragment budget.
    """

    __slots__ = (
        "_aborted",
        "_collection",
        "_file_indexer",
        "_indexer",
        "_max_workers",
        "_meta",
        "_progress",
        "_queue",
        "_records",
        "_registry",
        "_resolved",
        "_settings",
        "_store",
    )

    _store: ChunkStore
    _registry: SyncRegistry
    _settings: Settings
    _collection: str
    _resolved: Path
    _max_workers: int
    _progress: Callable[[str], None]
    _queue: Queue[WindowMsg]
    _meta: dict[str, FileMeta]
    _records: dict[str, FileRecord | None]
    _indexer: ProgressiveIndexer
    _file_indexer: SingleFileIndexer
    _aborted: bool

    def __new__(
        cls,
        store: ChunkStore,
        registry: SyncRegistry,
        settings: Settings,
        *,
        collection: str,
        resolved: Path,
        max_workers: int,
        progress: Callable[[str], None],
    ) -> Self:
        self = super().__new__(cls)
        self._store = store
        self._registry = registry
        self._settings = settings
        self._collection = collection
        self._resolved = resolved
        self._max_workers = max(1, max_workers)
        self._progress = progress
        self._queue = Queue(maxsize=max(2, self._max_workers * 2))
        self._meta = {}
        self._records = {}
        self._aborted = False
        self._file_indexer = SingleFileIndexer(
            store, registry, settings, collection=collection, resolved=resolved
        )
        self._indexer = ProgressiveIndexer(
            self, flush_bytes=settings.sync_flush_mb * 1024 * 1024
        )
        return self

    def build_records(
        self, chunks: list[Chunk], vectors: NDArray[np.float32]
    ) -> list[dict[str, object]]:
        return ChunkTable.build_records(chunks, vectors)

    def insert_records(self, records: list[dict[str, object]]) -> int:
        return self._store.insert_records(records)

    def on_flush(self, checkpoints: Sequence[FlushCheckpoint]) -> None:
        """Commit every touched file's watermark in one registry transaction (G4)."""
        for checkpoint in checkpoints:
            meta = self._meta[checkpoint.file_id]
            row = self._file_indexer.checkpoint_row(meta, checkpoint)
            self._registry.upsert_file(row, commit=False)
        self._registry.commit()

    def run(self, files: list[Path]) -> tuple[int, int, list[str]]:
        """Ingest *files* progressively; return ``(ingested, failed, errors)``."""
        if not files:
            return 0, 0, []
        # Pre-read registry rows here so producers never race the consumer's
        # writes on the shared sqlite connection (SQLITE_MISUSE).
        self._records = {str(fp): self._registry.get_file(str(fp)) for fp in files}
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            for file_path in files:
                executor.submit(self._produce, file_path)
            ingested, failed, errors = self._consume(len(files))
        if not self._aborted and not self._run_indexer(errors, self._indexer.drain):
            failed += 1
        return ingested, failed, errors

    def _run_indexer(self, errors: list[str], op: Callable[[], object]) -> bool:
        """Run one consumer op; on ANY failure abort and keep draining (liveness).

        A consumer error (flush or a raising progress callback) must abort without
        escaping ``_consume`` and dead-locking the executor on blocked producers.
        """
        try:
            op()
        except Exception as exc:  # noqa: BLE001 - consumer liveness: never escape
            self._abort(errors, f"flush failed: {exc}")
            return False
        return True

    def _abort(self, errors: list[str], message: str) -> None:
        """Abort the sync and roll back the partial registry transaction (G2/§5.3)."""
        self._aborted = True
        self._registry.rollback()
        errors.append(f"[{self._collection}] {message}")
        logger.exception("Flush failed for %s", self._collection)

    def _produce(self, file_path: Path) -> None:
        """Plan the file's resume watermark and stream its windows to the queue.

        Enqueues exactly one final sentinel in ``finally`` so a producer that died
        can never block the consumer — any exception becomes the file's error.
        """
        file_id = str(file_path)
        document_name = str(file_path.relative_to(self._resolved))
        error: str | None = None
        try:
            plan = self._file_indexer.plan_file(file_path, self._records.get(file_id))
            self._meta[file_id] = plan.meta
            streamer = DocumentStreamer(self._settings)
            for batch, vectors in streamer.stream_batches(
                plan.chunks, start_index=plan.meta.resume_watermark
            ):
                self._queue.put(WindowMsg(file_id, batch, vectors))
        except _RECOVERABLE as exc:
            logger.warning("Ingest failed for %s: %s", document_name, exc)
            error = f"{document_name}: {exc}"
        except Exception as exc:
            logger.exception("Unexpected ingest failure for %s", document_name)
            error = f"{document_name}: {exc}"
        finally:
            stale = error is not None and self._file_indexer.should_clear_stale(
                self._records.get(file_id), file_path
            )
            self._queue.put(
                WindowMsg(file_id, [], None, final=True, error=error, clear_stale=stale)
            )

    def _consume(self, n_files: int) -> tuple[int, int, list[str]]:
        """Drain windows into the indexer until every file sends its sentinel."""
        ingested = 0
        failed = 0
        errors: list[str] = []
        begun: set[str] = set()
        seen_final = 0
        while seen_final < n_files:
            msg = self._queue.get()
            if not msg.final:
                self._apply_window(msg, begun, errors)
                continue
            seen_final += 1
            if self._apply_final(msg, begun, errors) == "ingested":
                ingested += 1
            else:
                failed += 1
        return ingested, failed, errors

    def _apply_window(self, msg: WindowMsg, begun: set[str], errors: list[str]) -> None:
        """Feed one window to the indexer, unless the sync already aborted."""
        vectors = msg.vectors
        if self._aborted or vectors is None:
            return
        self._run_indexer(errors, partial(self._feed, msg, vectors, begun))

    def _feed(
        self, msg: WindowMsg, vectors: NDArray[np.float32], begun: set[str]
    ) -> None:
        self._ensure_begun(msg.file_id, begun)
        self._indexer.add_window(msg.file_id, msg.batch, vectors)

    def _apply_final(self, msg: WindowMsg, begun: set[str], errors: list[str]) -> str:
        """Handle a file's final sentinel; return ``"ingested"`` or ``"failed"``.

        Progress runs through _run_indexer so a raising callback aborts, not escapes.
        """
        if msg.error is not None:
            errors.append(msg.error)
            if msg.clear_stale:
                self._run_indexer(errors, partial(self._clear_stale, msg.file_id))
            self._run_indexer(errors, partial(self._progress, f"Failed {msg.error}"))
            return "failed"
        if self._aborted:
            errors.append(f"[{self._collection}] aborted before {msg.file_id}")
            return "failed"
        if not self._run_indexer(errors, partial(self._finish, msg, begun)):
            return "failed"
        self._run_indexer(errors, partial(self._progress, f"Ingested {msg.file_id}"))
        return "ingested"

    def _finish(self, msg: WindowMsg, begun: set[str]) -> None:
        self._ensure_begun(msg.file_id, begun)
        self._indexer.complete_file(msg.file_id)

    def _clear_stale(self, file_id: str) -> None:
        """Drop a changed file's stored chunks after a failed re-ingest (consumer).

        Runs on the single-writer consumer, never a producer, so it cannot race the
        LanceDB writes. The stored record names the exact scope to clear in Lance.
        """
        self._file_indexer.clear_stale(self._records.get(file_id))

    def _ensure_begun(self, file_id: str, begun: set[str]) -> None:
        """Reconcile-delete (on the consumer → single-writer) then register the file.

        watermark > 0 → delete-tail (G2, dedups a crash tail); else full overwrite.
        """
        if file_id in begun:
            return
        meta = self._meta[file_id]
        self._file_indexer.reconcile_store(meta)
        self._indexer.begin_file(
            file_id,
            resume_watermark=meta.resume_watermark,
            total_chunks=meta.total_chunks,
        )
        begun.add(file_id)
