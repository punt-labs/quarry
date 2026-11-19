"""Shared per-process state for the daemon: settings, database, embeddings, tasks.

``DaemonContext`` is the single object every route handler reaches through: it
lazily builds and caches the engine resources (via ``QuarryResources``) and owns
the ``TaskRegistry`` for background operations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.daemon.ingest_queue import IngestQueue
from quarry.daemon.runtime import DaemonRuntime
from quarry.daemon.tasks import TaskRegistry
from quarry.http_resources import QuarryResources

if TYPE_CHECKING:
    from quarry.backend_protocols import EmbeddingBackend
    from quarry.config import Settings
    from quarry.daemon.watch_loop import WatchLoop
    from quarry.db import Database
    from quarry.recall_telemetry import RecallTelemetry

# CORS origins allowed by default when the caller does not override them.
DEFAULT_CORS_ORIGINS = frozenset({"http://localhost"})


@final
class DaemonContext:
    """Shared state for the HTTP server: settings, database, embeddings, tasks."""

    _resources: QuarryResources
    _api_key: str | None
    _cors_origins: frozenset[str]
    _tasks: TaskRegistry
    _ingest_queue: IngestQueue
    _runtime: DaemonRuntime

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
        """Assemble lazy resources and loop-free queue and watch state.

        Args:
            api_key: Remote bearer credential; None leaves it unset.
            cors_origins: Allowed origins; an empty set uses the built-in defaults.
            embedder: Optional backend shared by warmup and queries instead of ONNX.
        """
        self = super().__new__(cls)
        self._resources = QuarryResources(settings, embedder=embedder)
        self._api_key = api_key
        self._cors_origins = cors_origins or DEFAULT_CORS_ORIGINS
        self._tasks = TaskRegistry()
        # Built here (no running loop needed — its workers start lazily on first
        # submit) so every route reaches one resident queue through the context.
        self._ingest_queue = IngestQueue(self)
        # Inert until start() (which needs a running loop + builds the observer),
        # so construction stays loop-free like the queue's.
        self._runtime = DaemonRuntime(self)
        return self

    @property
    def api_key(self) -> str | None:
        """Expose the configured remote bearer credential, if any."""
        return self._api_key

    @property
    def cors_origins(self) -> frozenset[str]:
        """Expose the origins admitted by the daemon's CORS policy."""
        return self._cors_origins

    @property
    def tasks(self) -> TaskRegistry:
        """Expose the resident registry of background-task states."""
        return self._tasks

    @property
    def ingest_queue(self) -> IngestQueue:
        """Return the resident serialized ingest queue."""
        return self._ingest_queue

    @property
    def watch_loop(self) -> WatchLoop:
        """Return the always-on filesystem watch loop."""
        return self._runtime.watch_loop

    async def start_watch_loop(self) -> None:
        """Start watching every roster database after daemon warmup."""
        await self._runtime.watch_loop.start()

    async def aclose_watch_loop(self) -> None:
        """Stop new filesystem jobs before the queue drains."""
        await self._runtime.watch_loop.stop()

    @property
    def recall(self) -> RecallTelemetry:
        """Return the telemetry facade for this daemon lifetime."""
        return self._runtime.recall

    @property
    def uptime_seconds(self) -> float:
        """Return seconds elapsed since the context was created."""
        return self._runtime.uptime_seconds

    @property
    def state(self) -> str:
        """Return ``"ready"`` once resources are warmed, else ``"starting"``.

        The daemon warms single-threaded before it accepts traffic, so a
        ``/health`` reporting ``"starting"`` means a request raced ahead of
        ``warm()`` — the state a client polls on before probing readiness.
        """
        return self._runtime.state

    @property
    def database(self) -> Database:
        """Open the daemon's write connection lazily on first access."""
        return self._resources.database

    @property
    def query_database(self) -> Database:
        """Open a separate query connection lazily on first access."""
        return self._resources.query_database

    @property
    def embedder(self) -> EmbeddingBackend:
        """Load the shared embedding backend lazily on first access."""
        return self._resources.embedder

    @property
    def settings(self) -> Settings:
        """Expose the settings shared by routes and background workers."""
        return self._resources.settings

    @property
    def database_name(self) -> str:
        """Return the name of the database this daemon is bound to at startup.

        The name is the directory beneath ``quarry_root`` that holds the
        LanceDB data dir (``.../<name>/lancedb``), matching what ``quarry
        databases`` lists.  It is the ``database`` half of every content job's
        :class:`RouteKey`, so a capture/remember/ingest routes to the same
        per-table worker the watch loop uses for that database.
        """
        return self._resources.settings.lancedb_path.parent.name or "default"

    def warm(self) -> None:
        """Build cached resources single-threaded before serving."""
        self._resources.warm()
        self._runtime.mark_ready()
