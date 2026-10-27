"""Directory sync: discover files, compute delta, ingest new/changed, delete removed."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from quarry.db import ChunkStore
from quarry.failure_reason import FailureReason
from quarry.ingestion.pipeline import SUPPORTED_EXTENSIONS
from quarry.sync_delete_reconciler import DeleteReconciler
from quarry.sync_discovery import FileDiscovery
from quarry.sync_file_store import FileRecord
from quarry.sync_ingest import CollectionIngestor
from quarry.sync_planner import SyncPlanner

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.sync_registry import SyncRegistry
    from quarry.types import LanceDB

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncContext:
    """Shared handles a single-collection sync threads through its helpers."""

    collection: str
    resolved: Path
    db: LanceDB
    conn: SyncRegistry
    progress: Callable[[str], None]


@dataclass(frozen=True, slots=True)
class SyncResult:
    collection: str
    ingested: int
    refreshed: int
    deleted: int
    skipped: int
    failed: int
    errors: list[str] = field(default_factory=list)


def _refresh_files(
    plan_to_refresh: list[tuple[Path, str]],
    ctx: SyncContext,
) -> tuple[int, int, list[str]]:
    """Update registry rows for files whose content hash still matches.

    No LanceDB work, no re-embedding — just a fresh ``(mtime, size,
    content_hash, ingested_at)`` for each row, committed as one unit. Re-hashes
    the file at refresh time to guard against TOCTOU: if the file changed since
    the plan was computed, the refresh is skipped so the next sync detects it.
    """
    refreshed = 0
    errors: list[str] = []
    for fp, plan_hash in plan_to_refresh:
        document_name = str(fp.relative_to(ctx.resolved))
        try:
            stat = fp.stat()
            current_hash = FileDiscovery.content_hash(fp)
            if current_hash != plan_hash:
                logger.info("File changed since plan, skipping refresh: %s", fp)
                continue
            ctx.conn.files.upsert_file(
                FileRecord(
                    path=str(fp),
                    collection=ctx.collection,
                    document_name=document_name,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    ingested_at=datetime.now(UTC).isoformat(),
                    content_hash=current_hash,
                ),
                commit=False,
            )
            refreshed += 1
            ctx.progress(f"[{ctx.collection}] Refreshed {document_name}")
        except OSError as exc:
            errors.append(FailureReason.line(document_name, exc))
            logger.warning("Refresh failed for %s: %s", fp, exc)
    ctx.conn.commit()
    return refreshed, len(errors), errors


def _delete_documents(
    plan_to_delete: list[str],
    ctx: SyncContext,
) -> tuple[int, int, list[str]]:
    """Delete documents from a sync plan, returning (deleted, failed, errors).

    Each deletion is a Lance delete plus a registry-row delete; the whole batch
    commits as one unit (idempotent — deleting an absent doc is a no-op).
    """
    t_delete_start = time.perf_counter()
    files_by_document_name: dict[str, list[FileRecord]] = {}
    for rec in ctx.conn.files.list_files(ctx.collection):
        files_by_document_name.setdefault(rec.document_name, []).append(rec)

    deleted = 0
    errors: list[str] = []
    for document_name in plan_to_delete:
        try:
            ChunkStore(ctx.db).delete_document(
                document_name, collection=ctx.collection, count=False
            )
            for rec in files_by_document_name.get(document_name, []):
                ctx.conn.files.delete_file(rec.path, commit=False)
            deleted += 1
            ctx.progress(f"[{ctx.collection}] Deleted {document_name}")
        except FailureReason.RECOVERABLE as exc:
            errors.append(FailureReason.line(document_name, exc))
            logger.exception("Delete failed for %s", document_name)
            ctx.progress(f"[{ctx.collection}] Failed to delete {document_name}")
    ctx.conn.commit()
    if plan_to_delete:
        logger.info(
            "sync: [%s] deleted %d documents in %.2fs",
            ctx.collection,
            deleted,
            time.perf_counter() - t_delete_start,
        )
    return deleted, len(errors), errors


def sync_collection(
    directory: Path,
    collection: str,
    db: LanceDB,
    settings: Settings,
    conn: SyncRegistry,
    *,
    max_workers: int = 1,
    progress_callback: Callable[[str], None] | None = None,
) -> SyncResult:
    """Sync a single registered directory with LanceDB.

    Computes the delta, removes deleted files, refreshes touched-but-unchanged
    files, then ingests new/changed/partial files through bounded progressive
    commit (DES-034): each flush writes a batch to LanceDB and commits one
    registry transaction, so a crash loses at most one in-flight window and the
    collection is searchable as it fills.

    Catches OSError, ValueError, RuntimeError, and TimeoutError for individual
    file ingest/delete failures so sync continues when one fails.
    """

    def _progress(msg: str) -> None:
        logger.info(msg)
        if progress_callback is not None:
            progress_callback(msg)

    t_sync_start = time.perf_counter()
    resolved = directory.resolve()
    ctx = SyncContext(collection, resolved, db, conn, _progress)

    t0 = time.perf_counter()
    plan = SyncPlanner(resolved, collection, conn, SUPPORTED_EXTENSIONS).compute()
    logger.info(
        "sync: [%s] plan computed in %.2fs", collection, time.perf_counter() - t0
    )
    # deletions_safe gates EVERY deletion path: when the disk view is unreliable
    # (root unresolvable/excluded, or the walk could not fully enumerate the tree)
    # neither the plan's to_delete NOR DeleteReconciler's LanceDB-vs-disk prune may
    # run — an incomplete scan is not evidence that documents were removed.
    to_delete = (
        DeleteReconciler(db, collection).to_delete(
            plan.to_delete, registry_tracked=plan.registry_tracked
        )
        if plan.deletions_safe
        else []
    )
    _progress(
        f"[{collection}] {len(plan.to_ingest)} to ingest, "
        f"{len(plan.to_refresh)} to refresh, "
        f"{len(to_delete)} to delete, {plan.unchanged} unchanged"
    )

    deleted, failed, errors = _delete_documents(to_delete, ctx)
    refreshed, ref_failed, ref_errors = _refresh_files(plan.to_refresh, ctx)
    failed += ref_failed
    errors.extend(ref_errors)

    ingested = 0
    if plan.to_ingest:
        ingestor = CollectionIngestor(
            ChunkStore(db),
            conn,
            settings,
            collection=collection,
            resolved=resolved,
            max_workers=max_workers,
            progress=_progress,
        )
        ingested, ing_failed, ing_errors = ingestor.run(plan.to_ingest)
        failed += ing_failed
        errors.extend(ing_errors)

    logger.info(
        "sync: [%s] completed in %.2fs"
        " (%d ingested, %d refreshed, %d deleted, %d skipped, %d failed)",
        collection,
        time.perf_counter() - t_sync_start,
        ingested,
        refreshed,
        deleted,
        plan.unchanged,
        failed,
    )
    return SyncResult(
        collection=collection,
        ingested=ingested,
        refreshed=refreshed,
        deleted=deleted,
        skipped=plan.unchanged,
        failed=failed,
        errors=errors,
    )
