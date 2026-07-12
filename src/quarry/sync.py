"""Directory sync: discover files, compute delta, ingest new/changed, delete removed."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from quarry.config import Settings
from quarry.db import ChunkStore
from quarry.ingestion.pipeline import SUPPORTED_EXTENSIONS
from quarry.sync_discovery import FileDiscovery
from quarry.sync_finalize import SyncFinalizer
from quarry.sync_ingest import CollectionIngestor
from quarry.sync_registry import FileRecord, SyncRegistry
from quarry.types import LanceDB

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncPlan:
    to_ingest: list[Path]
    to_refresh: list[tuple[Path, str]]
    to_delete: list[str]
    unchanged: int


@dataclass(frozen=True, slots=True)
class SyncContext:
    """Shared handles a single-collection sync threads through its helpers."""

    collection: str
    resolved: Path
    db: LanceDB
    conn: SyncRegistry
    progress: Callable[[str], None]


def _refresh_hash(
    file_path: Path,
    record: FileRecord,
    stat: os.stat_result,
) -> str | None:
    """Return the disk hash if *file_path* is a refresh (content unchanged), else None.

    A refresh means ``(mtime, size)`` shifted but the content hash still matches
    the stored value, so only the registry row needs updating — LanceDB is left
    alone. Missing stored hash, size mismatch, or a hash read error all decline.
    """
    if record.content_hash is None or record.size != stat.st_size:
        return None
    try:
        disk_hash = FileDiscovery.content_hash(file_path)
    except OSError:
        return None
    return disk_hash if disk_hash == record.content_hash else None


def compute_sync_plan(
    directory: Path,
    collection: str,
    conn: SyncRegistry,
    extensions: frozenset[str],
) -> SyncPlan:
    """Compare files on disk against the registry to produce a sync plan.

    Categorizes each discovered file into one of four buckets:

    - ``to_ingest``: new files, size mismatches, files whose content hash
      changed, or files with a partial resume watermark (mid-file, DES-034).
    - ``to_refresh``: files whose ``(mtime, size)`` shifted but whose content
      hash still matches — only the registry row is updated.
    - ``to_delete``: ``document_name``s present in the registry but no longer
      on disk.
    - ``unchanged``: files with identical ``(mtime, size)``.

    Fail-safe rules: size mismatch, missing stored hash, or hash read errors
    all fall through to ``to_ingest``.
    """
    discovery = FileDiscovery(directory)
    disk_files = discovery.discover(extensions)
    disk_paths = {str(p) for p in disk_files}
    known_files = {r.path: r for r in conn.list_files(collection)}

    to_ingest: list[Path] = []
    to_refresh: list[tuple[Path, str]] = []
    unchanged = 0

    for file_path in disk_files:
        stat = file_path.stat()
        record = known_files.get(str(file_path))
        if record is None or record.is_partial:
            to_ingest.append(file_path)
            continue
        if record.mtime == stat.st_mtime and record.size == stat.st_size:
            unchanged += 1
            continue
        refresh = _refresh_hash(file_path, record, stat)
        if refresh is not None:
            to_refresh.append((file_path, refresh))
        else:
            to_ingest.append(file_path)

    to_delete = [
        r.document_name for r in known_files.values() if r.path not in disk_paths
    ]
    return SyncPlan(
        to_ingest=to_ingest,
        to_refresh=to_refresh,
        to_delete=to_delete,
        unchanged=unchanged,
    )


@dataclass(frozen=True)
class SyncResult:
    collection: str
    ingested: int
    refreshed: int
    deleted: int
    skipped: int
    failed: int
    errors: list[str] = field(default_factory=list)


_RECOVERABLE = (OSError, ValueError, RuntimeError, TimeoutError)


def _refresh_files(
    plan_to_refresh: list[tuple[Path, str]],
    ctx: SyncContext,
) -> tuple[int, int, list[str]]:
    """Update registry rows for files whose content hash still matches.

    No LanceDB work, no re-embedding — just a fresh ``(mtime, size,
    content_hash, ingested_at)`` for each row, committed as one unit. Re-hashes
    the file at refresh time to guard against TOCTOU: if the file changed since
    ``compute_sync_plan``, the refresh is skipped so the next sync detects it.
    """
    refreshed = 0
    failed = 0
    errors: list[str] = []
    for fp, plan_hash in plan_to_refresh:
        try:
            stat = fp.stat()
            current_hash = FileDiscovery.content_hash(fp)
            if current_hash != plan_hash:
                logger.info("File changed since plan, skipping refresh: %s", fp)
                continue
            document_name = str(fp.relative_to(ctx.resolved))
            ctx.conn.upsert_file(
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
            failed += 1
            errors.append(f"{fp}: {exc}")
            logger.warning("Refresh failed for %s: %s", fp, exc)
    ctx.conn.commit()
    return refreshed, failed, errors


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
    for rec in ctx.conn.list_files(ctx.collection):
        files_by_document_name.setdefault(rec.document_name, []).append(rec)

    deleted = 0
    failed = 0
    errors: list[str] = []
    for document_name in plan_to_delete:
        try:
            ChunkStore(ctx.db).delete_document(
                document_name, collection=ctx.collection, count=False
            )
            for rec in files_by_document_name.get(document_name, []):
                ctx.conn.delete_file(rec.path, commit=False)
            deleted += 1
            ctx.progress(f"[{ctx.collection}] Deleted {document_name}")
        except _RECOVERABLE as exc:
            failed += 1
            errors.append(f"{document_name}: {exc}")
            logger.exception("Delete failed for %s", document_name)
            ctx.progress(f"[{ctx.collection}] Failed to delete {document_name}: {exc}")
    ctx.conn.commit()
    if plan_to_delete:
        logger.info(
            "sync: [%s] deleted %d documents in %.2fs",
            ctx.collection,
            deleted,
            time.perf_counter() - t_delete_start,
        )
    return deleted, failed, errors


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
    plan = compute_sync_plan(resolved, collection, conn, SUPPORTED_EXTENSIONS)
    logger.info(
        "sync: [%s] plan computed in %.2fs", collection, time.perf_counter() - t0
    )
    _progress(
        f"[{collection}] {len(plan.to_ingest)} to ingest, "
        f"{len(plan.to_refresh)} to refresh, "
        f"{len(plan.to_delete)} to delete, {plan.unchanged} unchanged"
    )

    deleted, failed, errors = _delete_documents(plan.to_delete, ctx)
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


def sync_all(
    db: LanceDB,
    settings: Settings,
    *,
    max_workers: int = 1,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, SyncResult]:
    """Sync all registered directories.

    Opens the registry, iterates all registrations, syncs each,
    then optimizes the LanceDB table.
    """
    t_all_start = time.perf_counter()
    conn = SyncRegistry(settings.registry_path)
    try:
        registrations = conn.list_registrations()
        results: dict[str, SyncResult] = {}
        for reg in registrations:
            results[reg.collection] = sync_collection(
                Path(reg.directory),
                reg.collection,
                db,
                settings,
                conn,
                max_workers=max_workers,
                progress_callback=progress_callback,
            )
        SyncFinalizer(db, settings).run()
        logger.info(
            "sync: all collections completed in %.2fs",
            time.perf_counter() - t_all_start,
        )
        return results
    finally:
        conn.close()
