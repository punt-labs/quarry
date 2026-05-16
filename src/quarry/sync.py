"""Directory sync: discover files, compute delta, ingest new/changed, delete removed."""

from __future__ import annotations

import gc
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from quarry.models import Chunk

from quarry.config import Settings
from quarry.db import ChunkStore, TableOptimizer
from quarry.ingestion.pipeline import SUPPORTED_EXTENSIONS, prepare_document
from quarry.sync_discovery import FileDiscovery
from quarry.sync_registry import FileRecord, SyncRegistry
from quarry.types import LanceDB

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncConfig:
    """Configuration for a directory sync operation."""

    directory: Path
    collection: str
    max_workers: int = 4


@dataclass(frozen=True)
class SyncPlan:
    to_ingest: list[Path]
    to_refresh: list[tuple[Path, str]]
    to_delete: list[str]
    unchanged: int


def compute_sync_plan(
    directory: Path,
    collection: str,
    conn: SyncRegistry,
    extensions: frozenset[str],
) -> SyncPlan:
    """Compare files on disk against the registry to produce a sync plan.

    Categorizes each discovered file into one of four buckets:

    - ``to_ingest``: new files, size mismatches, or files whose content
      hash has changed.  These need full re-embedding.
    - ``to_refresh``: files whose ``(mtime, size)`` shifted but whose
      content hash still matches the stored value.  Only the registry
      row needs updating — LanceDB is left alone.  Each entry carries
      the freshly-computed hash so the refresh helper can reuse it.
    - ``to_delete``: ``document_name``s present in the registry but no
      longer on disk.
    - ``unchanged``: files with identical ``(mtime, size)``.

    Fail-safe rules: size mismatch, missing stored hash, or hash read
    errors all fall through to ``to_ingest``.  We never put a file in
    ``to_refresh`` unless we are certain its content matches.
    """
    discovery = FileDiscovery(directory)
    disk_files = discovery.discover(extensions)
    disk_paths = {str(p) for p in disk_files}

    # Single query: load all known files for this collection into a dict
    known_files = {r.path: r for r in conn.list_files(collection)}

    to_ingest: list[Path] = []
    to_refresh: list[tuple[Path, str]] = []
    unchanged = 0

    for file_path in disk_files:
        stat = file_path.stat()
        record = known_files.get(str(file_path))
        if record is None:
            to_ingest.append(file_path)
            continue
        if record.mtime == stat.st_mtime and record.size == stat.st_size:
            unchanged += 1
            continue
        # mtime or size changed — consult content hash if we have one.
        if record.content_hash is not None and record.size == stat.st_size:
            try:
                disk_hash = FileDiscovery.content_hash(file_path)
            except OSError:
                to_ingest.append(file_path)
                continue
            if disk_hash == record.content_hash:
                to_refresh.append((file_path, disk_hash))
                continue
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


def _ingest_files(
    plan_to_ingest: list[Path],
    resolved: Path,
    collection: str,
    db: LanceDB,
    settings: Settings,
    conn: SyncRegistry,
    max_workers: int,
    progress: Callable[[str], None],
) -> tuple[int, int, list[str], list[tuple[list[Chunk], NDArray[np.float32]]]]:
    """Ingest files from a sync plan.

    Returns ``(ingested, failed, errors, chunk_batch)`` where *chunk_batch*
    is a list of ``(chunks, vectors)`` pairs ready for a single batched
    ``table.add()`` call.  The caller (``sync_collection``) performs the
    batch write after all documents have been processed.

    Per-document deletes (for overwrite semantics) happen inside each
    worker thread.  Per-document embedding happens in the worker thread
    too, but the results are accumulated and written in one shot at the
    end to reduce LanceDB fragment churn.

    Registry rows are written with ``commit=False`` — the caller
    (``sync_collection``) commits after ``batch_insert_chunks`` succeeds,
    so a crash between prepare and batch-write rolls back the registry
    and the next sync re-processes those files.
    """
    ingested = 0
    failed = 0
    errors: list[str] = []
    chunk_batch: list[tuple[list[Chunk], NDArray[np.float32]]] = []

    def _timed_prepare(
        fp: Path, document_name: str
    ) -> tuple[
        float,
        os.stat_result,
        str | None,
        tuple[list[Chunk], NDArray[np.float32]] | None,
    ]:
        t = time.perf_counter()
        # Delete existing chunks for overwrite semantics.
        ChunkStore(db).delete_document(
            document_name, collection=collection, count=False
        )
        # Chunk + embed without writing to LanceDB.
        # Agent memory params (agent_handle, memory_type, summary) are not
        # passed — directory sync does not support per-document memory tagging.
        # See DES-018 for the agent memory design.
        prepared = prepare_document(
            fp,
            settings,
            collection=collection,
            document_name=document_name,
        )
        elapsed = time.perf_counter() - t
        stat = fp.stat()
        try:
            content_hash: str | None = FileDiscovery.content_hash(fp)
        except OSError:
            content_hash = None
        return elapsed, stat, content_hash, prepared

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _timed_prepare,
                fp,
                str(fp.relative_to(resolved)),
            ): fp
            for fp in plan_to_ingest
        }
        for future in as_completed(futures):
            fp = futures[future]
            document_name = str(fp.relative_to(resolved))
            try:
                elapsed, stat, content_hash, prepared = future.result()
                conn.upsert_file(
                    FileRecord(
                        path=str(fp),
                        collection=collection,
                        document_name=document_name,
                        mtime=stat.st_mtime,
                        size=stat.st_size,
                        ingested_at=datetime.now(UTC).isoformat(),
                        content_hash=content_hash,
                    ),
                    commit=False,
                )
                if prepared is not None:
                    chunk_batch.append(prepared)
                    ingested += 1
                    progress(
                        f"[{collection}] Ingested {document_name} in {elapsed:.2f}s"
                    )
                else:
                    progress(f"[{collection}] No chunks from {document_name}")
            except _RECOVERABLE as exc:
                failed += 1
                errors.append(f"{document_name}: {exc}")
                logger.exception("Ingest failed for %s", document_name)
                progress(f"[{collection}] Failed {document_name}: {exc}")
    return ingested, failed, errors, chunk_batch


def _refresh_files(
    plan_to_refresh: list[tuple[Path, str]],
    resolved: Path,
    collection: str,
    conn: SyncRegistry,
    progress: Callable[[str], None],
) -> tuple[int, int, list[str]]:
    """Update registry rows for files whose content hash still matches.

    No LanceDB work, no re-embedding — just a fresh ``(mtime, size,
    content_hash, ingested_at)`` for each row.

    Re-hashes the file at refresh time to guard against TOCTOU: if the
    file changed between ``compute_sync_plan`` and now, the refresh is
    skipped so the old registry row stays and the next sync detects the
    mtime mismatch again.

    Registry rows are written with ``commit=False`` — the caller
    (``sync_collection``) commits after ``batch_insert_chunks`` succeeds.

    Returns ``(refreshed, failed, errors)`` matching the pattern used
    by ``_ingest_files`` and ``_delete_documents``.
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
            document_name = str(fp.relative_to(resolved))
            conn.upsert_file(
                FileRecord(
                    path=str(fp),
                    collection=collection,
                    document_name=document_name,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    ingested_at=datetime.now(UTC).isoformat(),
                    content_hash=current_hash,
                ),
                commit=False,
            )
            refreshed += 1
            progress(f"[{collection}] Refreshed {document_name}")
        except OSError as exc:
            failed += 1
            errors.append(f"{fp}: {exc}")
            logger.warning("Refresh failed for %s: %s", fp, exc)
    return refreshed, failed, errors


def _delete_documents(
    plan_to_delete: list[str],
    collection: str,
    db: LanceDB,
    conn: SyncRegistry,
    progress: Callable[[str], None],
) -> tuple[int, int, list[str]]:
    """Delete documents from a sync plan, returning (deleted, failed, errors).

    Registry rows are deleted with ``commit=False`` — the caller
    (``sync_collection``) commits after ``batch_insert_chunks`` succeeds.
    """
    t_delete_start = time.perf_counter()
    # Pre-build lookup for O(1) path resolution during deletes
    files_by_document_name: dict[str, list[FileRecord]] = {}
    for rec in conn.list_files(collection):
        files_by_document_name.setdefault(rec.document_name, []).append(rec)

    deleted = 0
    failed = 0
    errors: list[str] = []
    for document_name in plan_to_delete:
        try:
            ChunkStore(db).delete_document(
                document_name, collection=collection, count=False
            )
            for rec in files_by_document_name.get(document_name, []):
                conn.delete_file(rec.path, commit=False)
            deleted += 1
            progress(f"[{collection}] Deleted {document_name}")
        except _RECOVERABLE as exc:
            failed += 1
            errors.append(f"{document_name}: {exc}")
            logger.exception("Delete failed for %s", document_name)
            progress(f"[{collection}] Failed to delete {document_name}: {exc}")
    if plan_to_delete:
        logger.info(
            "sync: [%s] deleted %d documents in %.2fs",
            collection,
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

    Computes the delta, ingests new/changed files in parallel,
    removes deleted files, and updates the registry.

    Catches OSError, ValueError, RuntimeError, and TimeoutError for
    individual file ingest/delete failures so sync continues when one fails.
    """

    def _progress(msg: str) -> None:
        logger.info(msg)
        if progress_callback is not None:
            progress_callback(msg)

    t_sync_start = time.perf_counter()

    resolved = directory.resolve()
    t0 = time.perf_counter()
    plan = compute_sync_plan(resolved, collection, conn, SUPPORTED_EXTENSIONS)
    logger.info(
        "sync: [%s] plan computed in %.2fs",
        collection,
        time.perf_counter() - t0,
    )
    _progress(
        f"[{collection}] {len(plan.to_ingest)} to ingest, "
        f"{len(plan.to_refresh)} to refresh, "
        f"{len(plan.to_delete)} to delete, {plan.unchanged} unchanged"
    )

    ingested = 0
    refreshed = 0
    failed = 0
    errors: list[str] = []
    chunk_batch: list[tuple[list[Chunk], NDArray[np.float32]]] = []

    if plan.to_ingest:
        ingested, failed, errors, chunk_batch = _ingest_files(
            plan.to_ingest,
            resolved,
            collection,
            db,
            settings,
            conn,
            max_workers,
            _progress,
        )

    if plan.to_refresh:
        refreshed, ref_failed, ref_errors = _refresh_files(
            plan.to_refresh,
            resolved,
            collection,
            conn,
            _progress,
        )
        failed += ref_failed
        errors.extend(ref_errors)

    del_count, del_failed, del_errors = _delete_documents(
        plan.to_delete,
        collection,
        db,
        conn,
        _progress,
    )
    deleted = del_count
    failed += del_failed
    errors.extend(del_errors)

    # Batch-write all accumulated chunks in a single LanceDB transaction.
    if chunk_batch:
        t0 = time.perf_counter()
        total_inserted = ChunkStore(db).batch_insert(chunk_batch)
        logger.info(
            "sync: [%s] batch-inserted %d chunks in %.2fs",
            collection,
            total_inserted,
            time.perf_counter() - t0,
        )

    # Release numpy arrays promptly — chunk_batch holds all vectors
    # for this collection and can be hundreds of MiB on large syncs.
    if chunk_batch:
        del chunk_batch
        gc.collect(0)

    # Commit registry rows AFTER the batch insert succeeds.  Ingest rows
    # are written with commit=False so a crash between prepare and
    # batch-write rolls back the registry — the next sync re-processes
    # those files instead of silently losing chunks.
    conn.commit()

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
        t0 = time.perf_counter()
        opt = TableOptimizer(db)
        opt.create_collection_index()
        logger.info("sync: create_collection_index in %.2fs", time.perf_counter() - t0)
        t0 = time.perf_counter()
        opt.optimize()
        logger.info("sync: optimize_table in %.2fs", time.perf_counter() - t0)
        logger.info(
            "sync: all collections completed in %.2fs",
            time.perf_counter() - t_all_start,
        )

        t_gc = time.perf_counter()
        gc.collect(2)
        gc_elapsed = time.perf_counter() - t_gc
        rss_str = "unknown"
        try:
            with Path("/proc/self/status").open() as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_str = line.split(":")[1].strip()
                        break
        except OSError:
            pass
        logger.info("sync: post-sync GC in %.2fs, RSS: %s", gc_elapsed, rss_str)

        return results
    finally:
        conn.close()
