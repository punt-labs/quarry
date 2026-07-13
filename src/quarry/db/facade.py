"""Facade composing all database subsystems behind a single entry point."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Self

from quarry.db.chunk_catalog import ChunkCatalog
from quarry.db.chunk_search import ChunkSearch
from quarry.db.chunk_store import ChunkStore
from quarry.db.connection import LanceConnection
from quarry.db.optimizer import TableOptimizer
from quarry.db.schema import SchemaManager
from quarry.db.storage import get_db
from quarry.types import LanceDB


class Database:
    """Single entry point to the LanceDB storage subsystem."""

    __slots__ = ("_catalog", "_db", "_optimizer", "_schema", "_search", "_store")

    _db: LanceDB
    _store: ChunkStore
    _search: ChunkSearch
    _catalog: ChunkCatalog
    _schema: SchemaManager
    _optimizer: TableOptimizer

    def __new__(cls, db: LanceDB) -> Self:
        self = super().__new__(cls)
        self._db = db
        self._store = ChunkStore(db)
        self._search = ChunkSearch(db)
        self._catalog = ChunkCatalog(db)
        self._schema = SchemaManager(db)
        self._optimizer = TableOptimizer(db)
        return self

    @property
    def db(self) -> LanceDB:
        """Underlying LanceDB connection for pipeline interop."""
        return self._db

    @property
    def store(self) -> ChunkStore:
        """Chunk insert, delete, and count operations."""
        return self._store

    @property
    def search(self) -> ChunkSearch:
        """Vector similarity, hybrid BM25+vector, and RRF fusion."""
        return self._search

    @property
    def catalog(self) -> ChunkCatalog:
        """List documents, collections, and page text."""
        return self._catalog

    @property
    def schema(self) -> SchemaManager:
        """Schema creation, migration, and FTS index management."""
        return self._schema

    @property
    def optimizer(self) -> TableOptimizer:
        """Table compaction, FTS rebuild, and collection indexing."""
        return self._optimizer

    def ensure_schema(self) -> None:
        """Delegate to SchemaManager to ensure the chunks table exists."""
        self._schema.ensure()

    @classmethod
    def connect(cls, path: Path) -> Database:
        """Compose a Database over a self-recycling connection (see LanceConnection)."""
        return cls(LanceConnection(partial(get_db, path)))
