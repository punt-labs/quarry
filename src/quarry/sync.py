from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from quarry.registry import get_file, list_files


def discover_files(
    directory: Path,
    extensions: frozenset[str],
) -> list[Path]:
    """Recursively find files matching *extensions* under *directory*.

    Returns absolute resolved paths, sorted for deterministic order.
    """
    return sorted(
        child.resolve()
        for child in directory.rglob("*")
        if child.is_file() and child.suffix.lower() in extensions
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

    to_ingest: list[Path] = []
    unchanged = 0

    for file_path in disk_files:
        stat = file_path.stat()
        record = get_file(conn, str(file_path))
        if (
            record is None
            or record.mtime != stat.st_mtime
            or record.size != stat.st_size
        ):
            to_ingest.append(file_path)
        else:
            unchanged += 1

    db_files = list_files(conn, collection)
    to_delete = [r.document_name for r in db_files if r.path not in disk_paths]

    return SyncPlan(
        to_ingest=to_ingest,
        to_delete=to_delete,
        unchanged=unchanged,
    )
