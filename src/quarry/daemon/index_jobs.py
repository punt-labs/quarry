"""The watch loop's index jobs: per-file reindex, document delete, and bulk scan.

Each is an :class:`~quarry.daemon.ingest_unit.IngestUnit` the always-on watch
loop submits to the ingest queue as a *producer*.  They write no LanceDB table
directly beyond the single-file and bulk-sync cores they compose — the queue's
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

from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.settlement import Settlement
from quarry.daemon.store_run import StoreRun
from quarry.daemon.terminal import Terminal
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
    """Reindex one changed file through the single-file core.

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
        # Terminal so an UNEXPECTED failure records ``failed`` and never
        # escapes to crash the queue worker (the queue's per-job contract).
        with Terminal.for_job(state).recording():
            store = StoreRun.start(self, state, self._index, self._settlement)
            JobLifecycle(state).settle(await store.wait())

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: the file on disk is the recoverable artifact."""
        return None

    @staticmethod
    def _settlement(outcome: FileIndexOutcome) -> Settlement:
        """Return how *outcome* settles the job: its error is its own."""
        if outcome.error is not None:
            return Settlement.failed(outcome.error)
        return Settlement.done(
            {"document": outcome.document_name, "ingested": outcome.ingested}
        )

    def _index(self) -> FileIndexOutcome:
        """Open a thread-bound registry, index the file, then close it."""
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
    """Drop one or more documents' chunks and registry rows (same worker as insert).

    Riding the same per-``(database, collection)`` worker as the inserts means a
    delete can never interleave — or, for a deregister purge, be *resurrected*
    by — an insert for the same collection: it runs FIFO behind every admitted
    index/scan job, so a queued ``FileIndexJob`` for a just-removed collection
    re-inserts *before* this purge deletes, never after (the single-writer
    invariant a direct out-of-queue purge would violate).  ``documents`` carries
    one name for a live delete event, or the whole removed set for a deregister
    purge — one job, one admission slot, not one per document.
    """

    database: Database
    settings: Settings
    collection: str
    documents: tuple[str, ...]

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Delete every document's chunks + registry rows off-thread."""
        del ctx
        with Terminal.for_job(state).recording():
            store = StoreRun.start(self, state, self._delete, self._settlement)
            JobLifecycle(state).settle(await store.wait())

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: a delete has no content to recover."""
        return None

    def _settlement(self, deleted: int) -> Settlement:
        """Return the settlement of a delete that removed *deleted* chunks."""
        return Settlement.done({"documents": len(self.documents), "deleted": deleted})

    def _delete(self) -> int:
        """Delete chunks for every document, then their registry rows, in one txn."""
        deleted = sum(
            self.database.store.delete_document(name, collection=self.collection)
            for name in self.documents
        )
        wanted = set(self.documents)
        conn = SyncRegistry(self.settings.registry_path)
        try:
            for rec in conn.files.list_files(self.collection):
                if rec.document_name in wanted:
                    conn.files.delete_file(rec.path, commit=False)
            conn.commit()
        finally:
            conn.close()
        return deleted


@dataclass(frozen=True, slots=True)
class CollectionSyncJob:
    """A bulk scan of one collection — the unchanged ``CollectionIngestor``.

    Used for the initial index on start, an explicit full ``quarry sync``, and a
    fs-event burst above ``watch_bulk_threshold``.  One job per
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
        with Terminal.for_job(state).recording():
            store = StoreRun.start(self, state, self._sync, self._settlement)
            JobLifecycle(state).settle(await store.wait())

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: a bulk scan reconciles from disk, nothing to spool."""
        return None

    @staticmethod
    def _settlement(result: SyncResult) -> Settlement:
        """Return the settlement of a bulk sync that counted *result*."""
        return Settlement.done(
            {
                "collection": result.collection,
                "ingested": result.ingested,
                "refreshed": result.refreshed,
                "deleted": result.deleted,
                "skipped": result.skipped,
                "failed": result.failed,
                "errors": list(result.errors),
            }
        )

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
