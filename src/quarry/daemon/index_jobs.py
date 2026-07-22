"""The watch loop's index jobs: per-file reindex, document delete, and bulk scan.

Each is an :class:`~quarry.daemon.ingest_unit.IngestUnit` the always-on watch
loop (DES-045) submits to the DES-042 queue as a *producer*.  They write no
LanceDB table directly beyond the DES-034 core they compose — the queue's
per-``(database, collection)`` FIFO worker is still the single writer, and the
embed gate still bounds concurrency.  Unlike the content jobs (remember /
capture / URL ingest, which target the daemon's active database), these carry
their own :class:`~quarry.db.Database` connection and per-database
:class:`~quarry.config.Settings` so the loop can index every database in the
roster on that database's own persistent connection.

Kept separate from ``ingest_jobs.py`` (content ingest) per SRP: this module is
filesystem-index work, that one is inbound-content work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool

from quarry.ingestion.file_indexer import SingleFileIndexer
from quarry.sync import sync_collection
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.config import Settings
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.job_spool import SpoolRecord
    from quarry.daemon.tasks import TaskState
    from quarry.db import Database
    from quarry.ingestion.file_indexer import FileIndexOutcome
    from quarry.sync import SyncResult


@dataclass(frozen=True, slots=True)
class FileIndexJob:
    """Reindex one changed file through the DES-034 single-file core.

    ``spool_record`` returns ``None``: the file on disk is the durable artifact
    (like a capture's ``.md``), recoverable by a rescan, so a drain-abort need
    not spool it.
    """

    database: Database
    settings: Settings
    collection: str
    resolved_root: Path
    path: Path

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Index the file off-thread, recording per-file success or a soft error."""
        del ctx  # this job carries its own database; the queue's ctx is unused
        outcome = await run_in_threadpool(self._index)
        if outcome.error is not None:
            state.status = "failed"
            state.error = outcome.error
            return
        state.status = "completed"
        state.results = {
            "document": outcome.document_name,
            "ingested": outcome.ingested,
        }

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: the file on disk is the recoverable artifact."""
        return None

    def _index(self) -> FileIndexOutcome:
        """Open a thread-bound registry, index the file, then close it (DES-026)."""
        conn = SyncRegistry(self.settings.registry_path)
        try:
            indexer = SingleFileIndexer(
                self.database.store,
                conn,
                self.settings,
                collection=self.collection,
                resolved=self.resolved_root,
            )
            return indexer.index_one(self.path)
        finally:
            conn.close()


@dataclass(frozen=True, slots=True)
class DocumentDeleteJob:
    """Drop a removed file's chunks and its registry rows (same worker as insert).

    Riding the same per-``(database, collection)`` worker as the inserts means a
    delete can never interleave an insert for the same document.
    """

    database: Database
    settings: Settings
    collection: str
    document_name: str

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Delete the document's chunks + registry rows off-thread."""
        del ctx
        deleted = await run_in_threadpool(self._delete)
        state.status = "completed"
        state.results = {"document": self.document_name, "deleted": deleted}

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: a delete has no content to recover."""
        return None

    def _delete(self) -> int:
        """Delete chunks then the matching registry rows in one transaction."""
        deleted = self.database.store.delete_document(
            self.document_name, collection=self.collection
        )
        conn = SyncRegistry(self.settings.registry_path)
        try:
            for rec in conn.list_files(self.collection):
                if rec.document_name == self.document_name:
                    conn.delete_file(rec.path, commit=False)
            conn.commit()
        finally:
            conn.close()
        return deleted


@dataclass(frozen=True, slots=True)
class CollectionSyncJob:
    """A bulk scan of one collection — the unchanged DES-034 ``CollectionIngestor``.

    Used for the initial index on start, an explicit full ``quarry sync``, and a
    fs-event burst above ``watch_bulk_threshold`` (DES-045 §3.1).  One job per
    collection, not one per file, so the depth-32 admission bound and the
    fragment budget both hold.  The FTS rebuild is deferred to a following
    ``CollectionFinalizeJob`` (post-quiescence coalescing).
    """

    database: Database
    settings: Settings
    collection: str
    resolved_root: Path

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Run the bulk collection sync off-thread, recording its counts."""
        del ctx
        result = await run_in_threadpool(self._sync)
        state.status = "completed"
        state.results = {
            "collection": result.collection,
            "ingested": result.ingested,
            "refreshed": result.refreshed,
            "deleted": result.deleted,
            "skipped": result.skipped,
            "failed": result.failed,
            "errors": list(result.errors),
        }

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: a bulk scan reconciles from disk, nothing to spool."""
        return None

    def _sync(self) -> SyncResult:
        """Open a thread-bound registry, run the bulk sync, then close it."""
        conn = SyncRegistry(self.settings.registry_path)
        try:
            return sync_collection(
                self.resolved_root,
                self.collection,
                self.database.db,
                self.settings,
                conn,
            )
        finally:
            conn.close()
