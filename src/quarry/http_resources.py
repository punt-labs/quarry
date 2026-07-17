"""Daemon connection and embedding-session lifecycle for the HTTP server.

The ``quarryd`` daemon runs queries concurrently with a background sync
worker.  A shared LanceDB connection (sync write locks block readers) and a
shared ONNX session (``session.run()`` serialises callers via an internal mutex)
both cause head-of-line blocking.  This module isolates the query path with its
own read connection and ONNX session, distinct from the write connection and the
cached sync session.  See DES-032.
"""

from __future__ import annotations

import logging
from functools import cached_property
from typing import TYPE_CHECKING, Self

from quarry.db import Database
from quarry.ingestion.backends import new_embedding_backend

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.types import EmbeddingBackend

logger = logging.getLogger(__name__)


class QuarryResources:
    """Lazily-constructed DB connections and ONNX session for one daemon.

    Each resource is built once and cached.  The write connection (``database``),
    query read connection (``query_database``), and query ONNX session
    (``embedder``) are separate instances so sync cannot block queries (DES-032).
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

        ``cached_property`` is not thread-safe, so all shared state is built on
        the main thread to avoid request-time races (DES-032).  Each phase logs
        distinctly so a failure is attributed to the resource that failed.
        """
        logger.info("Warming write database connection...")
        _ = self.database
        logger.info("Warming isolated query database connection...")
        _ = self.query_database
        logger.info("Loading query ONNX embedding session...")
        _ = self.embedder
        logger.info("Daemon resources ready")
