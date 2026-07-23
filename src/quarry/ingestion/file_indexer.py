"""The per-file DES-034 index core: plan, reconcile, stream, and checkpoint one file.

``SingleFileIndexer`` owns the operations a single document's ingest performs —
extract + plan chunks, decide the within-file resume watermark, reconcile the
stored chunks (delete-tail for a resume, full overwrite otherwise), and build
the registry checkpoint row.  Two callers compose it:

- the bulk :class:`~quarry.sync_ingest.CollectionIngestor`, which drives the
  planning/reconcile/row steps per file against ONE cross-file
  ``ProgressiveIndexer`` so a large collection keeps DES-034's fragment budget;
- the watch loop's ``FileIndexJob`` (DES-045), which calls :meth:`index_one`
  for a single changed file with its own indexer — cheap, because an incremental
  edit touches a handful of files, never the whole collection.

Extracting this core is the reason the two paths share one tested implementation
instead of duplicating the delete-tail/overwrite and resume-watermark logic.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, final

from quarry.db.chunk_table import ChunkTable, DocumentRef
from quarry.ingestion.pipeline import plan_file_chunks
from quarry.ingestion.progressive import ProgressiveIndexer
from quarry.ingestion.streaming import DocumentStreamer
from quarry.sync_discovery import FileDiscovery
from quarry.sync_messages import FileMeta
from quarry.sync_registry import FileRecord
from quarry.sync_resume import ResumePolicy

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import numpy as np
    from numpy.typing import NDArray

    from quarry.config import Settings
    from quarry.db.chunk_store import ChunkStore
    from quarry.ingestion.progressive import FlushCheckpoint
    from quarry.models import Chunk
    from quarry.sync_registry import SyncRegistry

logger = logging.getLogger(__name__)

# The same recoverable failures ``CollectionIngestor`` tolerates per file: a
# vanished/unreadable file or an unsupported format is one file's error, never a
# crash of the surrounding sync or watch job.
_RECOVERABLE = (sqlite3.Error, OSError, ValueError, RuntimeError, TimeoutError)


@final
@dataclass(frozen=True, slots=True)
class FilePlan:
    """One file's ingest plan: its chunks, resume meta, and content hash."""

    document_name: str
    chunks: list[Chunk]
    meta: FileMeta
    content_hash: str | None


@final
@dataclass(frozen=True, slots=True)
class FileIndexOutcome:
    """The result of indexing one file: chunks written, or a per-file error.

    ``error`` is ``None`` on success and a human-readable reason otherwise (a
    vanished or unreadable file — a normal TOCTOU outcome the watch loop expects,
    not a crash), so the caller reports it without a broad ``except``.
    """

    document_name: str
    ingested: int
    error: str | None


@final
class SingleFileIndexer:
    """Index one file through the DES-034 core, shared by bulk sync and the watch loop.

    Doubles as the :class:`~quarry.ingestion.progressive.FlushTarget` for the
    single-file :meth:`index_one` path: ``_pending`` names the file whose rows a
    flush checkpoints, set transiently for the duration of one ``index_one`` call
    (single-threaded, so no lock is needed).  The bulk path never sets it — it
    drives :meth:`plan_file`, :meth:`reconcile_store`, and :meth:`checkpoint_row`
    against its own cross-file indexer instead.
    """

    __slots__ = (
        "_collection",
        "_pending",
        "_policy",
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
    _policy: ResumePolicy
    _pending: FileMeta | None

    def __new__(
        cls,
        store: ChunkStore,
        registry: SyncRegistry,
        settings: Settings,
        *,
        collection: str,
        resolved: Path,
    ) -> Self:
        self = super().__new__(cls)
        self._store = store
        self._registry = registry
        self._settings = settings
        self._collection = collection
        self._resolved = resolved
        self._policy = ResumePolicy()
        self._pending = None
        return self

    @property
    def collection(self) -> str:
        """Return the LanceDB collection this indexer writes."""
        return self._collection

    def plan_file(self, file_path: Path, record: FileRecord | None) -> FilePlan:
        """Extract, chunk, hash, and resolve *file_path*'s resume watermark.

        *record* is the file's current registry row (``None`` for a new file),
        pre-fetched by the caller so the bulk path never races the consumer's
        registry writes on a shared connection.
        """
        document_name = str(file_path.relative_to(self._resolved))
        chunks, deterministic = plan_file_chunks(
            file_path,
            self._settings,
            collection=self._collection,
            document_name=document_name,
        )
        content_hash = self._safe_hash(file_path)
        watermark = self._policy.resume_watermark(
            record, content_hash, len(chunks), deterministic=deterministic
        )
        meta = FileMeta(
            record=self._build_record(file_path, document_name, content_hash),
            resume_watermark=watermark,
            total_chunks=len(chunks),
        )
        return FilePlan(document_name, chunks, meta, content_hash)

    def reconcile_store(self, meta: FileMeta) -> None:
        """Clear the stored chunks before re-embedding (the single-writer step).

        ``watermark > 0`` → delete-tail (dedups a crash tail, keeps the confirmed
        prefix); otherwise a full overwrite (DES-034 §5.3 / G2).  Must run on the
        single writer for this table — the bulk consumer thread, or the watch
        loop's per-``(database, collection)`` worker.
        """
        document_name, watermark = meta.record.document_name, meta.resume_watermark
        if watermark > 0:
            self._store.delete_document_tail(
                DocumentRef(document_name, self._collection, watermark)
            )
        else:
            self._store.delete_document(
                document_name, collection=self._collection, count=False
            )

    def checkpoint_row(self, meta: FileMeta, checkpoint: FlushCheckpoint) -> FileRecord:
        """Build *meta*'s registry row at *checkpoint*'s committed watermark."""
        partial_hash = self._policy.partial_mark(checkpoint, meta.record.content_hash)
        return replace(
            meta.record,
            chunks_committed=checkpoint.chunks_committed,
            partial_hash=partial_hash,
        )

    def should_clear_stale(self, record: FileRecord | None, file_path: Path) -> bool:
        """Return whether a failed (re-)ingest must drop *record*'s stale chunks."""
        return self._policy.clear_stale_on_failure(record, self._safe_hash(file_path))

    def clear_stale(self, record: FileRecord | None) -> None:
        """Delete *record*'s stored chunks (the single-writer stale-drop step)."""
        if record is not None:
            self._store.delete_document(
                record.document_name, collection=self._collection, count=False
            )

    def index_one(self, file_path: Path) -> FileIndexOutcome:
        """Fully (re-)index one file, committing its registry row on flush.

        Runs the whole per-file path with its own ``ProgressiveIndexer``: plan,
        reconcile the store, stream+embed in bounded windows, then drain.  A file
        that vanished or cannot be read since the event is a graceful per-file
        error (its stale chunks are dropped when the content no longer matches),
        never a raised exception (bug-class 1: filesystem TOCTOU).
        """
        document_name = str(file_path.relative_to(self._resolved))
        file_id = str(file_path)
        record = self._registry.get_file(file_id)
        try:
            plan = self.plan_file(file_path, record)
        except _RECOVERABLE as exc:
            return self._plan_failed(document_name, record, file_path, exc)
        indexer = ProgressiveIndexer(
            self, flush_bytes=self._settings.sync_flush_mb * 1024 * 1024
        )
        self._pending = plan.meta
        try:
            self.reconcile_store(plan.meta)
            indexer.begin_file(
                file_id,
                resume_watermark=plan.meta.resume_watermark,
                total_chunks=plan.meta.total_chunks,
            )
            streamer = DocumentStreamer(self._settings)
            for batch, vectors in streamer.stream_batches(
                plan.chunks, start_index=plan.meta.resume_watermark
            ):
                indexer.add_window(file_id, batch, vectors)
            indexer.complete_file(file_id)
            indexer.drain()
        except _RECOVERABLE as exc:
            # Honor the contract: a mid-stream I/O/DB error (the file changed or
            # vanished, a flush failed) is a graceful per-file failure, not a
            # raise. The partial write is left for the next reconcile to
            # overwrite (its registry row reads partial → re-ingested).
            logger.warning("Watch index failed for %s: %s", document_name, exc)
            return FileIndexOutcome(
                document_name, indexer.inserted_count, error=f"{document_name}: {exc}"
            )
        finally:
            self._pending = None
        return FileIndexOutcome(document_name, indexer.inserted_count, error=None)

    def _plan_failed(
        self,
        document_name: str,
        record: FileRecord | None,
        file_path: Path,
        exc: Exception,
    ) -> FileIndexOutcome:
        """Record a planning failure, dropping now-stale chunks when appropriate."""
        logger.warning("Watch index failed for %s: %s", document_name, exc)
        if self.should_clear_stale(record, file_path):
            self.clear_stale(record)
            self._registry.delete_file(str(file_path), commit=True)
        return FileIndexOutcome(document_name, 0, error=f"{document_name}: {exc}")

    # -- FlushTarget for the single-file index_one path ---------------------

    def build_records(
        self, chunks: list[Chunk], vectors: NDArray[np.float32]
    ) -> list[dict[str, object]]:
        """Build LanceDB row dicts for one embed window (FlushTarget half)."""
        return ChunkTable.build_records(chunks, vectors)

    def insert_records(self, records: list[dict[str, object]]) -> int:
        """Append one flush's rows to LanceDB (FlushTarget half)."""
        return self._store.insert_records(records)

    def on_flush(self, checkpoints: Sequence[FlushCheckpoint]) -> None:
        """Commit the single file's watermark row(s) in one registry transaction."""
        if self._pending is None:
            return
        for checkpoint in checkpoints:
            self._registry.upsert_file(
                self.checkpoint_row(self._pending, checkpoint), commit=False
            )
        self._registry.commit()

    def _build_record(
        self, file_path: Path, document_name: str, content_hash: str | None
    ) -> FileRecord:
        """Build the base registry row for *file_path* at its current disk state."""
        stat = file_path.stat()
        return FileRecord(
            path=str(file_path),
            collection=self._collection,
            document_name=document_name,
            mtime=stat.st_mtime,
            size=stat.st_size,
            ingested_at=datetime.now(UTC).isoformat(),
            content_hash=content_hash,
        )

    @staticmethod
    def _safe_hash(file_path: Path) -> str | None:
        """Return the file's content hash, or ``None`` when it cannot be read."""
        try:
            return FileDiscovery.content_hash(file_path)
        except OSError:
            return None
