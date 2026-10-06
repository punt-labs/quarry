"""The ``quarry mcp`` stdio server — a FastMCP client of ``quarryd`` (DES-031 v2.2).

Every tool body is a thin :class:`~quarry.client.QuarryClient` call over the
daemon's ``/v1`` REST API; this module imports **no engine** (no ``quarry.db``,
``embeddings``, ``ingestion``, or ``retrieval``), so ``import quarry.mcp_server``
and running ``quarry mcp`` load zero LanceDB/ONNX.  It mirrors vox's
``vox mcp`` → ``server.py`` → ``VoxClientSync`` shape: the MCP server is a client
of the resident daemon, never a second in-process engine.

The fourteen tools and their docstrings are the surface Claude Code sees; the
bodies changed (client calls, fire-and-forget 202s), the surface did not.
This module owns retrieval/write (``find``, ``ingest``, ``remember``,
``learn``), status, and database selection; ``list`` lives in the sibling
:mod:`quarry.mcp_catalog`, the document/collection lifecycle tools in
:mod:`quarry.mcp_documents`, and ``missions_sync`` in
:mod:`quarry.mcp_missions` — all four register from here so the surface
stays one registration call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from mcp.server.mcpserver import MCPServer

from quarry.api import IngestRequest, RememberRequest, SearchRequest
from quarry.client import QuarryClient, TargetResolver
from quarry.config import Settings
from quarry.db_pointer import SELECTION
from quarry.formatting import (
    format_insights,
    format_search_results,
    format_status,
    format_switch_summary,
)
from quarry.mcp_catalog import ResourceCatalog
from quarry.mcp_documents import DocumentTools
from quarry.mcp_guard import ToolGuard
from quarry.mcp_missions import MissionTools

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


mcp = MCPServer(
    "punt-quarry",
    instructions=(
        "Use find before WebSearch or WebFetch for research, or before "
        "answering a why/how/what-did-we-decide question. Prefer grep for "
        "symbol and value lookups; prefer find for meaning.\n\n"
        "Full triggers, scoping, and failure recovery: the quarry-recall "
        "(retrieve) and quarry-capture (persist) skills — the single "
        "source of truth this block points to rather than restates.\n\n"
        "All quarry tool output is pre-formatted plain text using unicode "
        "characters for alignment. Always emit quarry output verbatim — "
        "never reformat, never convert to markdown tables, never wrap "
        "in code fences or boxes."
    ),
)


@final
class McpTools:
    """The MCP tool surface, each tool a thin :class:`QuarryClient` call.

    Holds only the client factory it resolves per call (fresh connection per
    tool, matching vox) — no engine, no thread pool, no database.  A down daemon
    surfaces as a clean MCP error string via :meth:`ToolGuard.wrap`, never an
    in-process engine fallback.  Tests inject a factory returning a client over
    an ``ASGITransport`` so each tool round-trips through the real daemon
    handlers.
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
        """Attach every guarded tool to *server* under its wire name.

        ``list`` and ``use`` keep their short wire names; the rest register under
        the method name.  Every method is already ``@ToolGuard.wrap``-wrapped, so
        the registered tool and a direct call share the identical error boundary.
        """
        server.add_tool(self.find)
        server.add_tool(self.ingest)
        server.add_tool(self.remember)
        server.add_tool(self.learn)
        server.add_tool(self.status)
        server.add_tool(self.insights)
        server.add_tool(self.use_database, name="use")
        ResourceCatalog(self._connect).register(server)
        DocumentTools(self._connect).register(server)
        MissionTools(self._connect).register(server)

    @ToolGuard.wrap
    def find(
        self,
        query: str,
        limit: int = 10,
        document_filter: str = "",
        collection: str = "",
        page_type: str = "",
        source_format: str = "",
        agent_handle: str = "",
        memory_type: str = "",
    ) -> str:
        (
            """Use find before WebSearch or WebFetch for research, or before """
            """answering a why/how/what-did-we-decide question. """
            """Prefer grep for symbol and value lookups; prefer find for meaning.

        Combines vector similarity and BM25 full-text search via Reciprocal
        Rank Fusion (RRF) for better recall on both meaning and exact terms.

        Args:
            query: Natural language search query.
            limit: Maximum number of results (default 10, max 50).
            document_filter: Optional exact document name to filter by.
            collection: Optional collection name to search within.
            page_type: Optional content type filter (text, code, spreadsheet, etc.).
            source_format: Optional source format filter (.pdf, .py, .xlsx, etc.).
            agent_handle: Your own handle to recall only your memories (e.g.
                "rmh"); leave empty to search everything.
            memory_type: Optional memory type filter (fact, observation, lesson, etc.).
        """
        )
        if err := self._reject_blank(query, "query"):
            return err
        if limit <= 0:
            return f"Error: limit must be >= 1 (got {limit})."
        req = SearchRequest(
            query=query,
            limit=min(limit, 50),
            collection=collection,
            document=document_filter,
            page_type=page_type,
            source_format=source_format,
            agent_handle=agent_handle,
            memory_type=memory_type,
            surface="mcp",
        )
        resp = self._connect().search(req)
        return format_search_results(query, [hit.model_dump() for hit in resp.results])

    @ToolGuard.wrap
    def ingest(
        self,
        source: str,
        overwrite: bool = False,
        collection: str = "",
    ) -> str:
        (
            """Use when you have a URL to add to the knowledge base — a doc, """
            """an article, a spec.

        """
            """remember = a specific durable fact, ingest = a URL, learn = a """
            """distilled lesson that gets retrieval preference.

        Fetches a URL with smart sitemap discovery and single-page fallback.
        For local files and directories, use ``register_directory`` +
        ``sync_all_registrations`` — the daemon owns the filesystem, so there is
        no in-process file loader here.

        Returns immediately — the daemon indexes in the background.

        Args:
            source: HTTP(S) URL to ingest.
            overwrite: If true, replace existing data.
            collection: Collection name. Auto-derived if empty.
        """
        )
        if not source.startswith(("http://", "https://")):
            return (
                f"Error: {source!r} is not a URL. Use "
                "register_directory(directory=...) to track local files and "
                "directories, then sync_all_registrations()."
            )
        accepted = self._connect().ingest_url(
            IngestRequest(source=source, overwrite=overwrite, collection=collection)
        )
        return f"▶  Ingesting {source} (task {accepted.task_id})"

    @ToolGuard.wrap
    def remember(
        self,
        content: str,
        document_name: str,
        overwrite: bool = False,
        collection: str = "",
        format_hint: str = "auto",
        agent_handle: str = "",
        memory_type: str = "",
        summary: str = "",
    ) -> str:
        (
            """Use remember when you learn something durable — a decision, """
            """a gotcha, a non-obvious fact, a procedure — so it survives """
            """context compaction.

        """
            """remember = a specific durable fact, ingest = a URL, learn = a """
            """distilled lesson that gets retrieval preference.

        Call it at these five moments, not at the end and not never: a
        non-obvious root cause or gotcha (fact); a ratified design decision with
        its reason (fact); a repeatable how-to (procedure); a judgement you will
        revisit (opinion); and once before submitting a mission result
        (observation). Always pass your own agent_handle — the daemon cannot
        infer it, and a subagent's working directory resolves to the repo's
        leader, not to you.

        The daemon scrubs secrets/PII before indexing. Returns immediately —
        the daemon indexes in the background.

        Args:
            content: The text content to remember.
            document_name: Name for the document (e.g., 'notes.md').
            overwrite: If true, replace existing data for this document. If
                false, an existing document of this name is left untouched.
            collection: Collection name. Leave empty to route by agent_handle —
                ``memory-<handle>`` when a handle is given, else ``default``.
            format_hint: Format hint: 'auto', 'plain', 'markdown', 'latex'.
            agent_handle: Your own handle (e.g. "rmh") — the memory is filed
                under it and recalled by it.
            memory_type: Memory classification: fact, observation, opinion,
                procedure. ``'lesson'`` is reserved for the ``learn`` tool.
            summary: One-line summary of the content.
        """
        )
        if err := self._reject_blank(document_name, "document_name"):
            return err
        if err := self._reject_blank(content, "content"):
            return err
        # The MCP surface deliberately defaults overwrite=False (unlike the CLI and
        # the RememberRequest model default of True): an agent calling remember
        # should add, not silently replace. The value is always passed explicitly.
        accepted = self._connect().remember(
            RememberRequest(
                name=document_name,
                content=content,
                overwrite=overwrite,
                collection=collection,
                format_hint=format_hint,
                agent_handle=agent_handle,
                memory_type=memory_type,
                summary=summary,
            )
        )
        return f"▶  Remembering {document_name} (task {accepted.task_id})"

    @ToolGuard.wrap
    def learn(self, lesson: str, topic: str = "", name: str = "") -> str:
        (
            """Use learn to save a distilled lesson that should outrank """
            """ordinary results for related queries -- a rule, a convention, """
            """a "do it this way" insight, not a one-off fact.

        """
            """remember = a specific durable fact, ingest = a URL, learn = a """
            """distilled lesson that gets retrieval preference.

        The daemon scrubs secrets/PII before indexing, same as remember.
        Lessons are capped at 500 characters -- use remember for anything
        longer. Returns immediately -- the daemon indexes in the background.

        Args:
            lesson: The distilled lesson text (<= 500 chars).
            topic: Optional domain tag (e.g. "testing", "release-process").
            name: Optional user-visible slug for later reference.
        """
        )
        if err := self._reject_blank(lesson, "lesson"):
            return err
        accepted = self._connect().learn(lesson, topic=topic, name=name)
        return f"▶  Learning saved ({accepted.status}, task {accepted.task_id})"

    @ToolGuard.wrap
    def status(self) -> str:
        """Use to check how much is indexed before you search or ingest."""
        return format_status(self._connect().status().model_dump())

    @ToolGuard.wrap
    def insights(self) -> str:
        """Use to read your own recall stats: query volume, latency, recall mix."""
        return format_insights(self._connect().insights())

    @ToolGuard.wrap
    def use_database(self, name: str) -> str:
        """Use to point every other tool at a different named database.

        All tools (find, ingest, sync, etc.) will target the selected database's
        daemon until changed again. Use list(kind="databases") to see the
        database the daemon is fixed to.

        Only selects among LOCAL databases: while a remote target (QUARRY_URL or
        a 'quarry login') is active, the remote daemon is fixed to its own
        database and this has no effect.

        Args:
            name: Database name (e.g., 'coding', 'work'). Use 'default' for
                  the default database.
        """
        # A local db selection only governs the loopback run-dir target. Under a
        # remote/explicit target it is IGNORED by TargetResolver — so refuse
        # rather than report a switch that silently doesn't take effect and could
        # read or destroy data on the wrong (remote) daemon.
        if err := self._reject_blank(name, "name"):
            return err
        if not TargetResolver.selects_local_db():
            return (
                "Error: no local switch — a remote quarry target is active "
                "(QUARRY_URL or 'quarry login'). The remote daemon is fixed to "
                "its own database; 'use' only selects among local databases. Run "
                "'quarry logout' or unset QUARRY_URL to return to the local daemon."
            )
        previous = SELECTION.active() or "default"
        # Select the literal named db, "default" included — never fall through to
        # the persisted default, or use("default") would silently pick
        # whatever the CLI last persisted and the summary path would lie about the
        # target subsequent tools connect to. Validate before mutating:
        # resolve_db_paths raises ValueError on a bad name, leaving the db unchanged.
        resolved = Settings.load().resolve_db_paths(name)
        SELECTION.override(name)
        return format_switch_summary(previous, name, str(resolved.lancedb_path))

    @staticmethod
    def _reject_blank(value: str, label: str) -> str | None:
        # Returns an Error: string for a blank required arg, else None. None is
        # the documented "valid" signal (no error) — the caller proceeds on None,
        # so this is an appropriate Optional, not a give-up value.
        if value.strip():
            return None
        return f"Error: {label} is required (got an empty value)."

    @staticmethod
    def run_stdio(db_name: str | None = None) -> None:
        """Run the stdio MCP server, targeting *db_name* (the daemon's database).

        Logging is the launcher's job: ``quarry mcp`` (the only entry point)
        configures the stderr level before calling in, so this stays a pure
        "select the database and serve" step.
        """
        SELECTION.override(db_name or "")
        logger.info("Starting quarry MCP server (client tier)")
        mcp.run(transport="stdio")


_tools = McpTools()
_tools.register(mcp)
