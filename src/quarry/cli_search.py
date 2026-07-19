"""The ``quarry find`` command: hybrid search over the daemon's database.

A pure client-tier command — it builds one :class:`~quarry.api.SearchRequest`,
calls :meth:`QuarryClient.search`, and renders the ranked hits.  There is no
local-vs-remote fork and no engine: the request model is the single wire shape,
so every filter the CLI accepts travels to the daemon and every field the daemon
returns renders identically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.api import SearchRequest

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing

_TEXT_PREVIEW_CHARS = 300
_SIMILARITY_PRECISION = 4


@final
class SearchCli:
    """Register and serve ``quarry find`` around an injected plumbing bundle."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach the ``find`` command to the top-level *app*."""
        app.command(name="find")(self._p.cli_errors(self._find))

    def _find(
        self,
        query: Annotated[str, typer.Argument(help="Search query")],
        limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 10,
        document: Annotated[
            str, typer.Option("--document", "-d", help="Filter by document name")
        ] = "",
        collection: Annotated[
            str, typer.Option("--collection", "-c", help="Filter by collection")
        ] = "",
        page_type: Annotated[
            str,
            typer.Option("--page-type", help="Filter by content type (text, code)"),
        ] = "",
        source_format: Annotated[
            str,
            typer.Option("--source-format", help="Filter by source format (.pdf, .py)"),
        ] = "",
        agent_handle: Annotated[
            str, typer.Option("--agent-handle", help="Filter by agent handle")
        ] = "",
        memory_type: Annotated[
            str, typer.Option("--memory-type", help="Filter by memory type")
        ] = "",
    ) -> None:
        """Search indexed documents."""
        req = SearchRequest(
            query=query,
            limit=limit,
            collection=collection,
            document=document,
            page_type=page_type,
            source_format=source_format,
            agent_handle=agent_handle,
            memory_type=memory_type,
        )
        resp = self._p.client().search(req)
        hits = [hit.model_dump() for hit in resp.results]
        self._p.emit(hits, self._render(hits))

    @classmethod
    def _render(cls, hits: list[dict[str, object]]) -> str:
        """Render ranked hits as ``[doc p.N | type/format] (similarity: …)`` blocks."""
        lines: list[str] = []
        for hit in hits:
            meta = f"{hit.get('page_type', '')}/{hit.get('source_format', '')}"
            raw_similarity = float(str(hit.get("similarity", 0.0)))
            similarity = round(raw_similarity, _SIMILARITY_PRECISION)
            lines.append(
                f"\n[{hit.get('document_name', '')} p.{hit.get('page_number', '')}"
                f" | {meta}] (similarity: {similarity})"
            )
            lines.append(str(hit.get("text", ""))[:_TEXT_PREVIEW_CHARS])
        return "\n".join(lines)
