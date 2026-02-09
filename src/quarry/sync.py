from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from quarry.config import Settings
from quarry.database import (
    create_collection_index,
    delete_document,
    optimize_table,
)
from quarry.pipeline import SUPPORTED_EXTENSIONS, ingest_document
from quarry.registry import (
    FileRecord,
    delete_file,
    list_files,
    list_registrations,
    open_registry,
    upsert_file,
)
from quarry.types import LanceDB

logger = logging.getLogger(__name__)


def _is_hidden(path: Path, root: Path) -> bool:
    """True if any component from *root* to *path* starts with a dot."""
    return any(part.startswith(".") for part in path.relative_to(root).parts)


def discover_files(
    directory: Path,
    extensions: frozenset[str],
) -> list[Path]:
    """Recursively find files matching *extensions* under *directory*.

    Skips dotfiles, macOS resource forks (``._*``), and files inside
    hidden directories (``.Trash``, ``.git``, etc.).

    Returns absolute paths, sorted for deterministic order.  Uses
    ``absolute()`` rather than ``resolve()`` so that symlinks within
    the tree keep their in-tree path (``relative_to`` stays valid).
    """
    return sorted(
        child.absolute()
        for child in directory.rglob("*")
        if child.is_file()
        and child.suffix.lower() in extensions
        and not _is_hidden(child, directory)
    )


@dataclass(frozen=True)
class SyncPlan:
    to_ingest: list[Path]
    to_delete: list[str]
    unchanged: int


def compute_sync_plan(
    directory: Path,
    collection: str,
    conn: sqlite3.Connection,
    extensions: frozenset[str],
) -> SyncPlan:
    """Compare files on disk against the registry to produce a sync plan.

    Returns which files need ingesting (new or changed), which
    document_names should be deleted (removed from disk), and
    how many were unchanged.
    """
    disk_files = discover_files(directory, extensions)
    disk_paths = {str(p) for p in disk_files}

    # Single query: load all known files for this collection into a dict
    known_files = {r.path: r for r in list_files(conn, collection)}

    to_ingest: list[Path] = []
    unchanged = 0

    for file_path in disk_files:
        stat = file_path.stat()
        record = known_files.get(str(file_path))
        if (
            record is None
            or record.mtime != stat.st_mtime
            or record.size != stat.st_size
        ):
            to_ingest.append(file_path)
        else:
            unchanged += 1

    to_delete = [
        r.document_name for r in known_files.values() if r.path not in disk_paths
    ]

    return SyncPlan(
        to_ingest=to_ingest,
        to_delete=to_delete,
        unchanged=unchanged,
    )


@dataclass(frozen=True)
class SyncResult:
    collection: str
    ingested: int
    deleted: int
    skipped: int
    failed: int
    errors: list[str] = field(default_factory=list)


def sync_collection(
    directory: Path,
    collection: str,
    db: LanceDB,
    settings: Settings,
    conn: sqlite3.Connection,
    *,
    max_workers: int = 4,
    progress_callback: Callable[[str], None] | None = None,
) -> SyncResult:
    """Sync a single registered directory with LanceDB.

    Computes the delta, ingests new/changed files in parallel,
    removes deleted files, and updates the registry.
    """

    def _progress(msg: str) -> None:
        logger.info(msg)
        if progress_callback is not None:
            progress_callback(msg)

    resolved = directory.resolve()
    plan = compute_sync_plan(resolved, collection, conn, SUPPORTED_EXTENSIONS)
    _progress(
        f"[{collection}] {len(plan.to_ingest)} to ingest, "
        f"{len(plan.to_delete)} to delete, {plan.unchanged} unchanged"
    )

    ingested = 0
    failed = 0
    errors: list[str] = []

    if plan.to_ingest:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    ingest_document,
                    fp,
                    db,
                    settings,
                    overwrite=True,
                    collection=collection,
                    document_name=str(fp.relative_to(resolved)),
                ): fp
                for fp in plan.to_ingest
            }
            for future in as_completed(futures):
                fp = futures[future]
                doc_name = str(fp.relative_to(resolved))
                try:
                    future.result()
                    stat = fp.stat()
                    upsert_file(
                        conn,
                        FileRecord(
                            path=str(fp),
                            collection=collection,
                            document_name=doc_name,
                            mtime=stat.st_mtime,
                            size=stat.st_size,
                            ingested_at=datetime.now(UTC).isoformat(),
                        ),
                        commit=False,
                    )
                    ingested += 1
                    _progress(f"[{collection}] Ingested {doc_name}")
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    errors.append(f"{doc_name}: {exc}")
                    _progress(f"[{collection}] Failed {doc_name}: {exc}")

    # Pre-build lookup for O(1) path resolution during deletes
    files_by_doc_name: dict[str, list[FileRecord]] = {}
    for rec in list_files(conn, collection):
        files_by_doc_name.setdefault(rec.document_name, []).append(rec)

    deleted = 0
    for doc_name in plan.to_delete:
        try:
            delete_document(db, doc_name, collection=collection)
            for rec in files_by_doc_name.get(doc_name, []):
                delete_file(conn, rec.path, commit=False)
            deleted += 1
            _progress(f"[{collection}] Deleted {doc_name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            errors.append(f"{doc_name}: {exc}")
            _progress(f"[{collection}] Failed to delete {doc_name}: {exc}")

    conn.commit()

    return SyncResult(
        collection=collection,
        ingested=ingested,
        deleted=deleted,
        skipped=plan.unchanged,
        failed=failed,
        errors=errors,
    )


def sync_all(
    db: LanceDB,
    settings: Settings,
    *,
    max_workers: int = 4,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, SyncResult]:
    """Sync all registered directories.

    Opens the registry, iterates all registrations, syncs each,
    then optimizes the LanceDB table.
    """
    conn = open_registry(settings.registry_path)
    try:
        registrations = list_registrations(conn)
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
        create_collection_index(db)
        optimize_table(db)
        return results
    finally:
        conn.close()
