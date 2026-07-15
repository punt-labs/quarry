"""Shared per-process state for the daemon: settings, database, embeddings, tasks.

``DaemonContext`` is the single object every route handler reaches through: it
lazily builds and caches the engine resources (via ``QuarryResources``) and owns
the ``TaskRegistry`` for background operations.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.tasks import TaskRegistry
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
    _ready: bool

    def __new__(
        cls,
        settings: Settings,
        *,
        api_key: str | None = None,
        cors_origins: frozenset[str] | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._resources = QuarryResources(settings)
        self._api_key = api_key
        self._cors_origins = cors_origins or DEFAULT_CORS_ORIGINS
        self._start_time = time.monotonic()
        self._tasks = TaskRegistry()
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

    def warm(self) -> None:
        """Build cached resources single-threaded before serving (DES-032)."""
        self._resources.warm()
        self._ready = True
