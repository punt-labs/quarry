"""Turn one debounced delta batch into per-file index and delete jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.fs_events import FsEvent
from quarry.daemon.index_jobs import DocumentDeleteJob, FileIndexJob
from quarry.ingestion.pipeline import SUPPORTED_EXTENSIONS
from quarry.sync_discovery import FileDiscovery

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.config import Settings
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.debounce import FlushBatch
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.route_key import RouteKey
    from quarry.db import Database


@final
@dataclass(frozen=True, slots=True)
class DeltaOutcome:
    """What one delta batch did: the jobs the queue admitted and the events it shed."""

    admitted: int
    shed: tuple[FsEvent, ...]


@final
class DeltaSubmission:
    """Submit a delta batch's per-file jobs on the ingest queue."""

    __slots__ = ("_ctx",)

    _ctx: DaemonContext

    def __new__(cls, ctx: DaemonContext) -> Self:
        """Bind submissions to the daemon's task registry and ingest queue."""
        self = super().__new__(cls)
        self._ctx = ctx
        return self

    def submit(
        self, batch: FlushBatch, db: Database, settings: Settings, root: Path
    ) -> DeltaOutcome:
        """Submit each per-file index/delete job; report what was admitted or shed.

        The authoritative filter runs here, post-debounce (once per distinct path
        per window, off the observer thread): ``is_indexable`` resolves the real
        path and rejects a symlink escaping the tree (security) plus applies the
        ignore rules BEFORE any content is read; a delete gets lexical
        ``is_deletable``.  A rejected path is dropped, not re-armed.
        """
        discovery = FileDiscovery(root)
        admitted = 0
        failed: list[FsEvent] = []
        for path in batch.modified:
            if not discovery.is_indexable(path, SUPPORTED_EXTENSIONS):
                continue
            job = FileIndexJob(db, settings, batch.key.collection, root, path)
            if self._submit(batch.key, job, "index"):
                admitted += 1
            else:
                failed.append(FsEvent(path, deleted=False))
        for path in batch.deleted:
            if not discovery.is_deletable(path, SUPPORTED_EXTENSIONS):
                continue
            name = self._document_name(root, path)
            if name is None:
                continue
            delete = DocumentDeleteJob(db, settings, batch.key.collection, (name,))
            if self._submit(batch.key, delete, "delete"):
                admitted += 1
            else:
                failed.append(FsEvent(path, deleted=True))
        return DeltaOutcome(admitted, tuple(failed))

    def _submit(self, key: RouteKey, job: IngestUnit, kind: str) -> bool:
        """Submit *job*; drop its task record and return False if the queue is full."""
        state = self._ctx.tasks.begin(kind)
        if self._ctx.ingest_queue.try_submit(key, job, state):
            return True
        self._ctx.tasks.drop(state)
        return False

    @staticmethod
    def _document_name(root: Path, path: Path) -> str | None:
        """Return *path*'s registry document name (relative to *root*), or None."""
        try:
            return str(path.relative_to(root))
        except ValueError:
            return None
