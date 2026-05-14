"""LanceDB connection factory and database discovery utilities."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

from quarry.chunk_catalog import ChunkCatalog
from quarry.results import DatabaseSummary
from quarry.types import LanceDB

logger = logging.getLogger(__name__)


def get_db(db_path: Path) -> LanceDB:
    """Connect to a LanceDB database, creating it if needed."""
    import lancedb  # noqa: PLC0415

    db_path.mkdir(parents=True, exist_ok=True)
    return cast("LanceDB", lancedb.connect(str(db_path)))  # type: ignore[attr-defined]


def format_size(size_bytes: int) -> str:
    """Human-readable size string from byte count."""
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes == 1:
        return "1 byte"
    return f"{size_bytes} bytes"


def dir_size_bytes(path: Path) -> int:
    """Total size in bytes of a directory tree.

    Uses ``du`` for speed -- a single C-level filesystem walk is orders
    of magnitude faster than Python's ``rglob`` + ``stat`` for large
    trees (e.g. 59K lance files: <1s vs 30s).  Falls back to rglob if
    ``du`` is unavailable or fails.
    """
    import subprocess  # noqa: PLC0415

    try:
        # du -sb (Linux) gives exact bytes.
        result = subprocess.run(  # noqa: S603
            ["du", "-sb", "--", str(path)],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return int(result.stdout.split()[0])
        # macOS: -sb not supported. -sk reports disk usage (block-aligned),
        # not apparent file size. Acceptable for display-only size reporting.
        result = subprocess.run(  # noqa: S603
            ["du", "-sk", "--", str(path)],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return int(result.stdout.split()[0]) * 1024
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    # Fallback: Python rglob (slow but always works).
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def discover_databases(root: Path) -> list[DatabaseSummary]:
    """Scan *root* for named databases and return structured summaries."""
    results: list[DatabaseSummary] = []
    if not root.exists():
        return results
    for entry in sorted(root.iterdir()):
        lance_dir = entry / "lancedb"
        if not entry.is_dir() or not lance_dir.exists():
            continue
        db = get_db(lance_dir)
        catalog = ChunkCatalog(db)
        docs = catalog.list_documents()
        size_bytes = dir_size_bytes(lance_dir)
        results.append(
            DatabaseSummary(
                name=entry.name,
                document_count=len(docs),
                size_bytes=size_bytes,
                size_description=format_size(size_bytes),
            )
        )
    return results
