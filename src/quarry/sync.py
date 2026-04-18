"""Directory sync: discover files, compute delta, ingest new/changed, delete removed."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from quarry.models import Chunk

import pathspec

from quarry.config import Settings
from quarry.database import (
    batch_insert_chunks,
    create_collection_index,
    delete_document,
    optimize_table,
)
from quarry.pipeline import SUPPORTED_EXTENSIONS, prepare_document
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

_HASH_CHUNK_SIZE: Final[int] = 1 << 20  # 1 MiB


def _content_hash(path: Path) -> str:
    """Return a fast content hash of *path* for change detection.

    Uses ``blake2b`` with a 16-byte digest (128 bits) — several GB/s on
    modern CPUs, collision-resistant enough for incremental sync, and
    in stdlib (no new deps).
    """
    h = hashlib.blake2b(digest_size=16)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()


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

    Symlinks whose target resolves outside *directory* are dropped and
    logged as a warning.  A registered ``~/docs`` containing
    ``shadow -> /etc/shadow`` would otherwise let the sync walker
    ingest arbitrary files on the server.

    Returns absolute paths, sorted for deterministic order.  Uses
    ``absolute()`` rather than ``resolve()`` so that symlinks within
    the tree keep their in-tree path (``relative_to`` stays valid).
    """
    root_spec = _load_ignore_spec(directory)
    result: list[Path] = []
    try:
        root_resolved = directory.resolve(strict=True)
    except (OSError, RuntimeError):
        logger.warning("Cannot resolve registered root: %s", directory)
        return result

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
            if filepath.is_symlink() and not _symlink_inside_root(
                filepath, root_resolved
            ):
                continue
            result.append(filepath.absolute())

    return result


def _symlink_inside_root(link: Path, root_resolved: Path) -> bool:
    """Return True iff *link*'s target resolves inside *root_resolved*.

    Skips unresolvable symlinks and targets outside the registered root so
    a remote client cannot ingest ``/etc/shadow`` via a symlink trap.  All
    rejections are logged at WARNING so operators can spot exfiltration
    attempts in the server log.
    """
    try:
        target = link.resolve(strict=True)
    except (OSError, RuntimeError):
        logger.warning("Skipping unresolvable symlink: %s", link)
        return False
    try:
        target.relative_to(root_resolved)
    except ValueError:
        logger.warning(
            "Skipping symlink %s that escapes registered root: %s",
            link,
            target,
        )
        return False
    return True


@dataclass(frozen=True)
class SyncPlan:
    to_ingest: list[Path]
    to_refresh: list[tuple[Path, str]]
    to_delete: list[str]
    unchanged: int


def compute_sync_plan(
    directory: Path,
    collection: str,
    conn: sqlite3.Connection,
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
    disk_files = discover_files(directory, extensions)
    disk_paths = {str(p) for p in disk_files}

    # Single query: load all known files for this collection into a dict
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
        # mtime or size changed — consult content hash if we have one.
        if record.content_hash is not None and record.size == stat.st_size:
            try:
                disk_hash = _content_hash(file_path)
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
    conn: sqlite3.Connection,
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
        delete_document(db, document_name, collection=collection)
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
            content_hash: str | None = _content_hash(fp)
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
                upsert_file(
                    conn,
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
    conn: sqlite3.Connection,
    progress: Callable[[str], None],
) -> tuple[int, int, list[str]]:
    """Update registry rows for files whose content hash still matches.

    No LanceDB work, no re-embedding — just a fresh ``(mtime, size,
    content_hash, ingested_at)`` for each row.

    Re-hashes the file at refresh time to guard against TOCTOU: if the
    file changed between ``compute_sync_plan`` and now, the refresh is
    skipped so the old registry row stays and the next sync detects the
    mtime mismatch again.

    Returns ``(refreshed, failed, errors)`` matching the pattern used
    by ``_ingest_files`` and ``_delete_documents``.
    """
    refreshed = 0
    failed = 0
    errors: list[str] = []
    for fp, plan_hash in plan_to_refresh:
        try:
            stat = fp.stat()
            current_hash = _content_hash(fp)
            if current_hash != plan_hash:
                logger.info("File changed since plan, skipping refresh: %s", fp)
                continue
            document_name = str(fp.relative_to(resolved))
            upsert_file(
                conn,
                FileRecord(
                    path=str(fp),
                    collection=collection,
                    document_name=document_name,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    ingested_at=datetime.now(UTC).isoformat(),
                    content_hash=current_hash,
                ),
                commit=True,
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
                delete_file(conn, rec.path, commit=True)
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
        total_inserted = batch_insert_chunks(db, chunk_batch)
        logger.info(
            "sync: [%s] batch-inserted %d chunks in %.2fs",
            collection,
            total_inserted,
            time.perf_counter() - t0,
        )

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
