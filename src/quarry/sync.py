"""Directory sync: discover files, compute delta, ingest new/changed, delete removed."""

from __future__ import annotations

import gc
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from quarry.models import Chunk

from quarry.config import Settings
from quarry.db import ChunkStore, TableOptimizer
from quarry.ingestion.pipeline import SUPPORTED_EXTENSIONS, prepare_document
from quarry.sync_discovery import FileDiscovery
from quarry.sync_registry import (
    FileRecord,
    delete_file,
    list_files,
    list_registrations,
    open_registry,
    upsert_file,
)
from quarry.types import LanceDB

logger = logging.getLogger(__name__)

_RECOVERABLE: Final = (OSError, ValueError, RuntimeError, TimeoutError)


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


@dataclass(frozen=True)
class SyncResult:
    collection: str
    ingested: int
    refreshed: int
    deleted: int
    skipped: int
    failed: int
    errors: list[str] = field(default_factory=list)


# --- Public module-level functions (existing API, delegate to classes) ---


def discover_files(directory: Path, extensions: frozenset[str]) -> list[Path]:
    """Recursively find files matching *extensions* under *directory*."""
    return FileDiscovery(directory).discover(extensions)


def compute_sync_plan(
    directory: Path,
    collection: str,
    conn: sqlite3.Connection,
    extensions: frozenset[str],
) -> SyncPlan:
    """Compare files on disk against the registry to produce a sync plan."""
    return CollectionSyncer.compute_plan(directory, collection, conn, extensions)


def sync_collection(
    directory: Path,
    collection: str,
    db: LanceDB,
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    max_workers: int = 1,
    progress_callback: Callable[[str], None] | None = None,
) -> SyncResult:
    """Sync a single registered directory with LanceDB."""
    config = SyncConfig(
        directory=directory,
        collection=collection,
        max_workers=max_workers,
    )
    return CollectionSyncer(config, db, settings, conn).sync(
        progress_callback=progress_callback,
    )


def sync_all(
    db: LanceDB,
    settings: Settings,
    *,
    max_workers: int = 1,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, SyncResult]:
    """Sync all registered directories."""
    return CollectionSyncer.sync_all(
        db,
        settings,
        max_workers=max_workers,
        progress_callback=progress_callback,
    )


# --- CollectionSyncer class ---


class CollectionSyncer:
    """Sync a single registered directory with LanceDB."""

    __slots__ = ("_config", "_conn", "_db", "_settings")

    _config: SyncConfig
    _conn: sqlite3.Connection
    _db: LanceDB
    _settings: Settings

    def __new__(
        cls,
        config: SyncConfig,
        db: LanceDB,
        settings: Settings,
        conn: sqlite3.Connection,
    ) -> Self:
        self = super().__new__(cls)
        self._config = config
        self._db = db
        self._settings = settings
        self._conn = conn
        return self

    @property
    def config(self) -> SyncConfig:
        return self._config

    @staticmethod
    def compute_plan(
        directory: Path,
        collection: str,
        conn: sqlite3.Connection,
        extensions: frozenset[str],
    ) -> SyncPlan:
        """Compare files on disk against the registry to produce a sync plan."""
        discovery = FileDiscovery(directory)
        disk_files = discovery.discover(extensions)
        disk_paths = {str(p) for p in disk_files}

        known_files = {r.path: r for r in list_files(conn, collection)}

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

    def sync(
        self,
        *,
        progress_callback: Callable[[str], None] | None = None,
    ) -> SyncResult:
        """Compute delta, ingest/refresh/delete, update registry."""

        def _progress(msg: str) -> None:
            logger.info(msg)
            if progress_callback is not None:
                progress_callback(msg)

        t_sync_start = time.perf_counter()
        collection = self._config.collection

        resolved = self._config.directory.resolve()
        t0 = time.perf_counter()
        plan = self.compute_plan(resolved, collection, self._conn, SUPPORTED_EXTENSIONS)
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
            ingested, failed, errors, chunk_batch = self._ingest_files(
                plan.to_ingest, resolved, _progress
            )

        if plan.to_refresh:
            refreshed, ref_failed, ref_errors = self._refresh_files(
                plan.to_refresh, resolved, _progress
            )
            failed += ref_failed
            errors.extend(ref_errors)

        del_count, del_failed, del_errors = self._delete_documents(
            plan.to_delete, _progress
        )
        deleted = del_count
        failed += del_failed
        errors.extend(del_errors)

        if chunk_batch:
            t0 = time.perf_counter()
            total_inserted = ChunkStore(self._db).batch_insert(chunk_batch)
            logger.info(
                "sync: [%s] batch-inserted %d chunks in %.2fs",
                collection,
                total_inserted,
                time.perf_counter() - t0,
            )

        if chunk_batch:
            del chunk_batch
            gc.collect(0)

        self._conn.commit()

        logger.info(
            "sync: [%s] completed in %.2fs"
            " (%d ingested, %d refreshed, %d deleted,"
            " %d skipped, %d failed)",
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

    @classmethod
    def sync_all(
        cls,
        db: LanceDB,
        settings: Settings,
        *,
        max_workers: int = 1,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, SyncResult]:
        """Sync all registered directories."""
        t_all_start = time.perf_counter()
        conn = open_registry(settings.registry_path)
        try:
            registrations = list_registrations(conn)
            results: dict[str, SyncResult] = {}
            for reg in registrations:
                config = SyncConfig(
                    directory=Path(reg.directory),
                    collection=reg.collection,
                    max_workers=max_workers,
                )
                syncer = cls(config, db, settings, conn)
                results[reg.collection] = syncer.sync(
                    progress_callback=progress_callback,
                )
            t0 = time.perf_counter()
            opt = TableOptimizer(db)
            opt.create_collection_index()
            logger.info(
                "sync: create_collection_index in %.2fs",
                time.perf_counter() - t0,
            )
            t0 = time.perf_counter()
            opt.optimize()
            logger.info(
                "sync: optimize_table in %.2fs",
                time.perf_counter() - t0,
            )
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
            logger.info(
                "sync: post-sync GC in %.2fs, RSS: %s",
                gc_elapsed,
                rss_str,
            )

            return results
        finally:
            conn.close()

    def _ingest_files(
        self,
        plan_to_ingest: list[Path],
        resolved: Path,
        progress: Callable[[str], None],
    ) -> tuple[
        int,
        int,
        list[str],
        list[tuple[list[Chunk], NDArray[np.float32]]],
    ]:
        """Ingest files from a sync plan."""
        collection = self._config.collection
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
            ChunkStore(self._db).delete_document(
                document_name, collection=collection, count=False
            )
            prepared = prepare_document(
                fp,
                self._settings,
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

        with ThreadPoolExecutor(max_workers=self._config.max_workers) as executor:
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
                    upsert_file(
                        self._conn,
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
        self,
        plan_to_refresh: list[tuple[Path, str]],
        resolved: Path,
        progress: Callable[[str], None],
    ) -> tuple[int, int, list[str]]:
        """Update registry for files whose content hash matches."""
        collection = self._config.collection
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
                upsert_file(
                    self._conn,
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
        self,
        plan_to_delete: list[str],
        progress: Callable[[str], None],
    ) -> tuple[int, int, list[str]]:
        """Delete documents from a sync plan."""
        collection = self._config.collection
        t_delete_start = time.perf_counter()
        files_by_doc: dict[str, list[FileRecord]] = {}
        for rec in list_files(self._conn, collection):
            files_by_doc.setdefault(rec.document_name, []).append(rec)

        deleted = 0
        failed = 0
        errors: list[str] = []
        for document_name in plan_to_delete:
            try:
                ChunkStore(self._db).delete_document(
                    document_name,
                    collection=collection,
                    count=False,
                )
                for rec in files_by_doc.get(document_name, []):
                    delete_file(self._conn, rec.path, commit=False)
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
