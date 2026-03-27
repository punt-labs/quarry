"""Directory sync: discover files, compute delta, ingest new/changed, delete removed."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pathspec

from quarry.config import Settings
from quarry.database import (
    create_collection_index,
    delete_document,
    optimize_table,
)
from quarry.pipeline import SUPPORTED_EXTENSIONS, ingest_document
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

_DEFAULT_IGNORE_PATTERNS: Final[list[str]] = [
    "__pycache__/",
    "*.pyc",
    "node_modules/",
    ".venv/",
    "venv/",
    ".tox/",
    ".nox/",
    ".eggs/",
    "*.egg-info/",
    "dist/",
    "build/",
    ".DS_Store",
]


def _load_ignore_spec(directory: Path) -> pathspec.PathSpec:
    """Build a PathSpec from ``.gitignore``, ``.quarryignore``, and defaults.

    Reads ignore files from the root of *directory* only.  Patterns use
    standard ``.gitignore`` syntax (``gitignore``).
    """
    lines: list[str] = list(_DEFAULT_IGNORE_PATTERNS)
    for name in (".gitignore", ".quarryignore"):
        ignore_path = directory / name
        if ignore_path.is_file():
            lines.extend(ignore_path.read_text(encoding="utf-8").splitlines())
    return pathspec.PathSpec.from_lines("gitignore", lines)


def _read_local_ignore(dirpath: Path) -> pathspec.PathSpec | None:
    """Read ``.gitignore`` from *dirpath*, returning a PathSpec or None."""
    gitignore = dirpath / ".gitignore"
    if not gitignore.is_file():
        return None
    lines = gitignore.read_text(encoding="utf-8").splitlines()
    return pathspec.PathSpec.from_lines("gitignore", lines)


def discover_files(
    directory: Path,
    extensions: frozenset[str],
) -> list[Path]:
    """Recursively find files matching *extensions* under *directory*.

    Respects ``.gitignore`` (at every level), ``.quarryignore``, and
    hardcoded ignore patterns (``venv/``, ``node_modules/``, etc.).
    Skips dotfiles, macOS resource forks (``._*``), and files inside
    hidden directories (``.Trash``, ``.git``, etc.).

    Returns absolute paths, sorted for deterministic order.  Uses
    ``absolute()`` rather than ``resolve()`` so that symlinks within
    the tree keep their in-tree path (``relative_to`` stays valid).
    """
    root_spec = _load_ignore_spec(directory)
    result: list[Path] = []

    for dirpath_str, dirnames, filenames in os.walk(directory):
        dirpath = Path(dirpath_str)
        rel_dir = dirpath.relative_to(directory)
        local_spec = _read_local_ignore(dirpath) if dirpath != directory else None

        # Prune hidden and ignored directories (in-place for os.walk)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not d.startswith(".")
            and not root_spec.match_file(str(rel_dir / d) + "/")
            and (local_spec is None or not local_spec.match_file(d + "/"))
        )

        for filename in sorted(filenames):
            if filename.startswith((".", "._")):
                continue
            filepath = dirpath / filename
            if filepath.suffix.lower() not in extensions:
                continue
            rel_path = str(filepath.relative_to(directory))
            if root_spec.match_file(rel_path):
                continue
            if local_spec is not None and local_spec.match_file(filename):
                continue
            result.append(filepath.absolute())

    return result


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

    Compares both mtime and size: mtime can change without content change
    (e.g. touch), and size can change without mtime (rare but possible).
    Either difference triggers re-ingest. Returns which files need
    ingesting (new or changed), which document_names should be deleted
    (removed from disk), and how many were unchanged.
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


_RECOVERABLE = (OSError, ValueError, RuntimeError, TimeoutError)


def _ingest_files(
    plan_to_ingest: list[Path],
    resolved: Path,
    collection: str,
    db: LanceDB,
    settings: Settings,
    conn: sqlite3.Connection,
    max_workers: int,
    progress: Callable[[str], None],
) -> tuple[int, int, list[str]]:
    """Ingest files from a sync plan, returning (ingested, failed, errors)."""
    ingested = 0
    failed = 0
    errors: list[str] = []

    def _timed_ingest(fp: Path, document_name: str) -> float:
        t = time.perf_counter()
        ingest_document(
            fp,
            db,
            settings,
            overwrite=True,
            collection=collection,
            document_name=document_name,
        )
        return time.perf_counter() - t

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _timed_ingest,
                fp,
                str(fp.relative_to(resolved)),
            ): fp
            for fp in plan_to_ingest
        }
        for future in as_completed(futures):
            fp = futures[future]
            document_name = str(fp.relative_to(resolved))
            try:
                elapsed = future.result()
                stat = fp.stat()
                upsert_file(
                    conn,
                    FileRecord(
                        path=str(fp),
                        collection=collection,
                        document_name=document_name,
                        mtime=stat.st_mtime,
                        size=stat.st_size,
                        ingested_at=datetime.now(UTC).isoformat(),
                    ),
                    commit=False,
                )
                ingested += 1
                progress(f"[{collection}] Ingested {document_name} in {elapsed:.2f}s")
            except _RECOVERABLE as exc:
                failed += 1
                errors.append(f"{document_name}: {exc}")
                logger.exception("Ingest failed for %s", document_name)
                progress(f"[{collection}] Failed {document_name}: {exc}")
    return ingested, failed, errors


def _delete_documents(
    plan_to_delete: list[str],
    collection: str,
    db: LanceDB,
    conn: sqlite3.Connection,
    progress: Callable[[str], None],
) -> tuple[int, int, list[str]]:
    """Delete documents from a sync plan, returning (deleted, failed, errors)."""
    t_delete_start = time.perf_counter()
    # Pre-build lookup for O(1) path resolution during deletes
    files_by_document_name: dict[str, list[FileRecord]] = {}
    for rec in list_files(conn, collection):
        files_by_document_name.setdefault(rec.document_name, []).append(rec)

    deleted = 0
    failed = 0
    errors: list[str] = []
    for document_name in plan_to_delete:
        try:
            delete_document(db, document_name, collection=collection)
            for rec in files_by_document_name.get(document_name, []):
                delete_file(conn, rec.path, commit=False)
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
    conn: sqlite3.Connection,
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
        f"{len(plan.to_delete)} to delete, {plan.unchanged} unchanged"
    )

    ingested = 0
    failed = 0
    errors: list[str] = []

    if plan.to_ingest:
        ingested, failed, errors = _ingest_files(
            plan.to_ingest,
            resolved,
            collection,
            db,
            settings,
            conn,
            max_workers,
            _progress,
        )

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

    conn.commit()

    logger.info(
        "sync: [%s] completed in %.2fs"
        " (%d ingested, %d deleted, %d skipped, %d failed)",
        collection,
        time.perf_counter() - t_sync_start,
        ingested,
        deleted,
        plan.unchanged,
        failed,
    )

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
    max_workers: int = 1,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, SyncResult]:
    """Sync all registered directories.

    Opens the registry, iterates all registrations, syncs each,
    then optimizes the LanceDB table.
    """
    t_all_start = time.perf_counter()
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
        t0 = time.perf_counter()
        create_collection_index(db)
        logger.info("sync: create_collection_index in %.2fs", time.perf_counter() - t0)
        t0 = time.perf_counter()
        optimize_table(db)
        logger.info("sync: optimize_table in %.2fs", time.perf_counter() - t0)
        logger.info(
            "sync: all collections completed in %.2fs",
            time.perf_counter() - t_all_start,
        )
        return results
    finally:
        conn.close()
