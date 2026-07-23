"""Shared per-process state for the daemon: settings, database, embeddings, tasks.

``DaemonContext`` is the single object every route handler reaches through: it
lazily builds and caches the engine resources (via ``QuarryResources``) and owns
the ``TaskRegistry`` for background operations.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.ingest_queue import IngestQueue
from quarry.daemon.tasks import TaskRegistry
from quarry.daemon.watch_loop import WatchLoop
from quarry.http_resources import QuarryResources

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.db import Database
    from quarry.types import EmbeddingBackend

# CORS origins allowed by default when the caller does not override them.
DEFAULT_CORS_ORIGINS = frozenset({"http://localhost"})


@final
class DaemonContext:
    """Shared state for the HTTP server: settings, database, embeddings, tasks."""

    _resources: QuarryResources
    _api_key: str | None
    _cors_origins: frozenset[str]
    _start_time: float
    _tasks: TaskRegistry
    _ingest_queue: IngestQueue
    _watch_loop: WatchLoop
    _ready: bool

    def __new__(
        cls,
        settings: Settings,
        *,
        api_key: str | None = None,
        cors_origins: frozenset[str] | None = None,
        # Test seam: inject a stand-in embedder so a hermetic daemon skips ONNX.
        # Threaded into QuarryResources so warm() and queries both honor it.
        embedder: EmbeddingBackend | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._resources = QuarryResources(settings, embedder=embedder)
        self._api_key = api_key
        self._cors_origins = cors_origins or DEFAULT_CORS_ORIGINS
        self._start_time = time.monotonic()
        self._tasks = TaskRegistry()
        # Built here (no running loop needed — its workers start lazily on first
        # submit) so every route reaches one resident queue through the context.
        self._ingest_queue = IngestQueue(self)
        # Inert until start() (which needs a running loop + builds the observer),
        # so construction stays loop-free like the queue's.
        self._watch_loop = WatchLoop(self)
        self._ready = False
        return self

    @property
    def api_key(self) -> str | None:
        return self._api_key

    @property
    def cors_origins(self) -> frozenset[str]:
        return self._cors_origins

    @property
    def tasks(self) -> TaskRegistry:
        return self._tasks

    @property
    def ingest_queue(self) -> IngestQueue:
        """Return the resident serialized ingest queue."""
        return self._ingest_queue

    async def aclose_ingest_queue(self) -> None:
        """Drain and close the ingest queue on shutdown (bounded by settings)."""
        await self._ingest_queue.aclose(
            drain_timeout=self.settings.ingest_drain_timeout_s
        )

    @property
    def watch_loop(self) -> WatchLoop:
        """Return the always-on filesystem watch loop (DES-045)."""
        return self._watch_loop

    async def start_watch_loop(self) -> None:
        """Start watching every roster database (after warm() + queue start)."""
        await self._watch_loop.start()

    async def aclose_watch_loop(self) -> None:
        """Stop the watch loop BEFORE the queue drains (no new jobs mid-drain)."""
        await self._watch_loop.stop()

    @property
    def uptime_seconds(self) -> float:
        """Return seconds elapsed since the context was created."""
        return time.monotonic() - self._start_time

    @property
    def state(self) -> str:
        """Return ``"ready"`` once resources are warmed, else ``"starting"``.

        The daemon warms single-threaded before it accepts traffic, so a
        ``/health`` reporting ``"starting"`` means a request raced ahead of
        ``warm()`` — the state a client polls on before probing readiness.
        """
        return "ready" if self._ready else "starting"

    @property
    def database(self) -> Database:
        return self._resources.database

    @property
    def query_database(self) -> Database:
        return self._resources.query_database

    @property
    def embedder(self) -> EmbeddingBackend:
        return self._resources.embedder

    @property
    def settings(self) -> Settings:
        return self._resources.settings

    @property
    def database_name(self) -> str:
        """Return the name of the database this daemon is bound to at startup.

        The name is the directory beneath ``quarry_root`` that holds the
        LanceDB data dir (``.../<name>/lancedb``), matching what ``quarry
        databases`` lists.  It is the ``database`` half of every content job's
        :class:`RouteKey`, so a capture/remember/ingest routes to the same
        per-table worker the watch loop uses for that database (DES-045).
        """
        return self._resources.settings.lancedb_path.parent.name or "default"

    def warm(self) -> None:
        """Build cached resources single-threaded before serving (DES-032)."""
        self._resources.warm()
        self._ready = True
