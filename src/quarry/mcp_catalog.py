"""The ``list`` MCP tool — resource-catalog lookups, a sibling of ``McpTools``.

Holds only the client factory, matching ``McpTools`` and ``MissionTools``: no
engine, no database, a fresh connection resolved per call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.client import TargetResolver
from quarry.db_pointer import SELECTION
from quarry.formatting import (
    format_collections,
    format_databases,
    format_documents,
    format_registrations,
)
from quarry.mcp_guard import ToolGuard

if TYPE_CHECKING:
    from collections.abc import Callable

    from mcp.server.mcpserver import MCPServer

    from quarry.client import QuarryClient


@final
class ResourceCatalog:
    """The ``list`` tool: what's indexed, by kind.

    ``list_resources`` dispatches on ``kind`` to one of four methods, each a
    thin :class:`~quarry.client.QuarryClient` call formatted for the stdio
    surface. Grouping these as methods (rather than free functions keyed by a
    dict-of-bound-methods) keeps the kind → handler mapping an ordinary
    attribute lookup, not a runtime dispatch table (PY-OO-7).
    """

    __slots__ = ("_connect",)

    _connect: Callable[[], QuarryClient]

    def __new__(
        cls, connect: Callable[[], QuarryClient] = TargetResolver.connect
    ) -> Self:
        self = super().__new__(cls)
        self._connect = connect
        return self

    def register(self, server: MCPServer) -> None:
        """Attach the guarded tool to *server* under its short wire name."""
        server.add_tool(self.list_resources, name="list")

    @ToolGuard.wrap
    def list_resources(self, kind: str, collection: str = "") -> str:
        """Use to see what's already indexed before ingesting it again.

        Args:
            kind: What to list — "documents", "collections", "databases",
                  or "registrations".
            collection: Optional collection filter (only for kind="documents").
        """
        handler = {
            "documents": self._list_documents,
            "collections": self._list_collections,
            "databases": self._list_databases,
            "registrations": self._list_registrations,
        }.get(kind)
        if handler is None:
            return (
                f"Error: unknown kind {kind!r}. "
                "Use documents, collections, databases, or registrations."
            )
        return handler(collection)

    def _list_documents(self, collection: str) -> str:
        docs = self._connect().list_documents(collection)
        return format_documents([doc.model_dump() for doc in docs.documents])

    def _list_collections(self, _collection: str) -> str:
        cols = self._connect().list_collections()
        return format_collections([col.model_dump() for col in cols.collections])

    def _list_databases(self, _collection: str) -> str:
        dbs = self._connect().list_databases()
        current = SELECTION.active() or "default"
        return format_databases(
            [db.model_dump() for db in dbs.databases], current=current
        )

    def _list_registrations(self, _collection: str) -> str:
        regs = self._connect().list_registrations()
        return format_registrations([reg.model_dump() for reg in regs.registrations])
