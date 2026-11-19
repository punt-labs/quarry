"""Assemble the ``/status`` body from the daemon's database and registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.daemon.routes.status_storage import StatusStorage
from quarry.ingestion.provider import ProviderSelection
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext


@final
class StatusSnapshot:
    """One ``/status`` reading over the daemon's single database."""

    __slots__ = ("_ctx",)

    _ctx: DaemonContext

    def __new__(cls, ctx: DaemonContext) -> Self:
        """Bind status readings to the daemon's live context."""
        self = super().__new__(cls)
        self._ctx = ctx
        return self

    def payload(self) -> dict[str, object]:
        """Return the JSON body: counts, model, provider, and storage figures."""
        settings = self._ctx.settings
        cols = self._ctx.database.catalog.list_collections()
        return {
            "document_count": sum(c["document_count"] for c in cols),
            "collection_count": len(cols),
            "chunk_count": self._ctx.database.store.count(),
            "registered_directories": self._registered_directories(),
            "database_path": str(settings.lancedb_path),
            "embedding_model": settings.embedding_model,
            "provider": ProviderSelection.display_cached(),
            "embedding_dimension": settings.embedding_dimension,
            **StatusStorage(settings.lancedb_path, self._ctx.database.db).fields(),
        }

    def _registered_directories(self) -> int:
        """Count the registered sync directories (0 before any registry exists)."""
        path = self._ctx.settings.registry_path
        if not path.exists():
            return 0
        conn = SyncRegistry(path)
        try:
            return len(conn.list_registrations())
        finally:
            conn.close()
