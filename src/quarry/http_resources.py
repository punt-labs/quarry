"""Daemon connection and embedding-session lifecycle for the HTTP server.

The long-running ``quarry serve`` daemon runs queries concurrently with a
background sync worker.  Two shared resources cause head-of-line blocking
when sync and queries contend for them:

1. A single LanceDB connection — sync holds write locks that block readers.
2. A single ONNX session — ``session.run()`` serialises callers through an
   internal mutex, so a query stalls for the duration of each sync embedding
   batch.

This module isolates the query path with its own read connection and ONNX
session, distinct from the write connection and the cached sync session.
See DES-032.
"""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Self

from quarry.db import Database
from quarry.ingestion.backends import new_embedding_backend

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.types import EmbeddingBackend


class QuarryResources:
    """Lazily-constructed DB connections and ONNX session for one daemon.

    Each resource is built once on first access and cached for the daemon's
    lifetime.  The write connection (``database``), the query read connection
    (``query_database``), and the query ONNX session (``embedder``) are
    deliberately separate instances so sync work cannot block queries.
    See DES-032.
    """

    _settings: Settings

    def __new__(cls, settings: Settings) -> Self:
        self = super().__new__(cls)
        self._settings = settings
        return self

    @property
    def settings(self) -> Settings:
        """The Settings these resources were built from."""
        return self._settings

    @cached_property
    def database(self) -> Database:
        """Shared connection for writes, admin, and sync."""
        return Database.connect(self._settings.lancedb_path)

    @cached_property
    def query_database(self) -> Database:
        """Read connection for search, isolated from sync write locks (DES-032)."""
        return Database.connect(self._settings.lancedb_path)

    @cached_property
    def embedder(self) -> EmbeddingBackend:
        """ONNX session for queries, isolated from the sync worker (DES-032)."""
        return new_embedding_backend()

    def warm(self) -> None:
        """Resolve every cached resource single-threaded before serving.

        ``cached_property`` is not thread-safe, so all shared state must be
        built once on the main thread.  Without this, concurrent search
        requests would race to construct ``query_database`` and ``embedder``
        on first use (DES-032).
        """
        _ = self.database
        _ = self.query_database
        _ = self.embedder
