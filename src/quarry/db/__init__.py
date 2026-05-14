"""Database subsystem: LanceDB connection, schema, storage, search, and catalog."""

from __future__ import annotations

from quarry.db.chunk_catalog import ChunkCatalog as ChunkCatalog
from quarry.db.chunk_search import ChunkSearch as ChunkSearch
from quarry.db.chunk_store import ChunkStore as ChunkStore
from quarry.db.facade import Database as Database
from quarry.db.optimizer import TableOptimizer as TableOptimizer
from quarry.db.schema import SchemaManager as SchemaManager
from quarry.db.storage import (
    dir_size_bytes as dir_size_bytes,
    discover_databases as discover_databases,
    format_size as format_size,
    get_db as get_db,
)

__all__ = [
    "ChunkCatalog",
    "ChunkSearch",
    "ChunkStore",
    "Database",
    "SchemaManager",
    "TableOptimizer",
    "dir_size_bytes",
    "discover_databases",
    "format_size",
    "get_db",
]
