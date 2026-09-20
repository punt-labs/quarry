"""The directory-sync commands: ``sync``, ``register``, ``deregister``, ``status``,
``insights``.

All pure client calls against the daemon that owns the registry (DES-031 I2): the
CLI never touches a local ``SyncRegistry``.  The task-dispatching commands are
fire-and-forget (DES-001) — the daemon validates synchronously before the 202
(malformed body / 409-concurrent for sync; path guard for register; existence +
404 for deregister), so a rejection still exits non-zero via the shared decorator,
and only the index/purge processing is deferred.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Self, final

import typer

from quarry.api import DeregisterRequest, RegisterRequest
from quarry.formatting import format_status

if TYPE_CHECKING:
    from collections.abc import Mapping

    from quarry.cli_captures import CliPlumbing


@dataclass(frozen=True, slots=True)
class _Breakdown:
    """One ``quarry insights`` row breakdown: its title and wire field names."""

    title: str
    key: str
    label_field: str
    count_field: str


@final
class SyncCli:
    """Serve ``sync``/``register``/``deregister``/``status``/``insights``."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach ``sync``/``register``/``deregister``/``status``/``insights``."""
        app.command(name="sync")(self._p.cli_errors(self._sync))
        app.command(name="register")(self._p.cli_errors(self._register))
        app.command(name="deregister")(self._p.cli_errors(self._deregister))
        app.command(name="status")(self._p.cli_errors(self._status))
        app.command(name="insights")(self._p.cli_errors(self._insights))

    def _sync(
        self,
        workers: Annotated[
            int | None,
            typer.Option("--workers", "-w", help="Ignored — the daemon sizes workers"),
        ] = None,
    ) -> None:
        """Sync all registered directories (dispatch only).

        Fire-and-forget: returns the daemon's task id and exits immediately (no
        per-task poll command exists yet — that is the future ``--wait``). A 409
        "already in progress" raised by the daemon is mapped to exit 0 by the
        decorator.
        """
        if workers is not None and not self._p.is_quiet():
            self._p.err_console.print(
                "Warning: --workers is ignored; the daemon sizes its own workers.",
                style="yellow",
            )
        accepted = self._p.client().sync()
        self._p.emit(
            accepted.model_dump(), f"Sync {accepted.status}: task_id={accepted.task_id}"
        )

    def _register(
        self,
        directory: Annotated[Path, typer.Argument(help="Directory to register")],
        collection: Annotated[
            str,
            typer.Option("--collection", "-c", help="Collection name (default: dir)"),
        ] = "",
    ) -> None:
        """Register a directory for incremental sync (dispatch only).

        The path is resolved against $HOME and re-guarded by the daemon on its own
        filesystem (traversal + $HOME allowlist); the registry write is deferred.
        """
        resolved_path = directory.expanduser().resolve()
        resolved = str(resolved_path)
        # A filesystem-root path has an empty leaf; fall back to "root" so
        # register never dispatches an empty collection name (matches the
        # leaf rule in Registrations.unique_collection_name).
        col = collection or resolved_path.name or "root"
        accepted = self._p.client().register(
            RegisterRequest(directory=resolved, collection=col)
        )
        self._p.emit(
            accepted.model_dump(),
            f"Register {accepted.status}: task_id={accepted.task_id}",
        )

    def _deregister(
        self,
        collection: Annotated[str, typer.Argument(help="Collection to deregister")],
        keep_data: Annotated[
            bool, typer.Option("--keep-data", help="Keep indexed data in LanceDB")
        ] = False,
    ) -> None:
        """Remove a directory registration (dispatch only).

        The daemon drops the registry row and reports ``removed`` synchronously
        (a 404 if the collection is unknown); the chunk purge runs as a background
        task, so ``deleted_chunks`` is not awaited here.
        """
        accepted = self._p.client().deregister(
            DeregisterRequest(collection=collection, keep_data=keep_data)
        )
        self._p.emit(
            accepted.model_dump(),
            f"Deregistered collection {collection!r} ({accepted.removed} files); "
            f"chunk purge {accepted.status}: task_id={accepted.task_id}",
        )

    def _status(self) -> None:
        """Show database status: documents, chunks, storage, model info."""
        resp = self._p.client().status()
        data = resp.model_dump()
        self._p.emit(data, format_status(data))

    def _insights(self) -> None:
        """Show recall telemetry: query volume, latency, and recall breakdowns."""
        data = self._p.client().insights().model_dump()
        self._p.emit(data, self._render_insights(data))

    # One breakdown per insights row group -- explicit field names, not
    # derived, so _rows stays a single straight-line pass.
    _BREAKDOWNS: tuple[_Breakdown, ...] = (
        _Breakdown("Top empty queries", "top_empty_queries", "query_scrubbed", "count"),
        _Breakdown(
            "Per-collection hits", "per_collection_hits", "collection", "hit_count"
        ),
        _Breakdown(
            "Per-agent recall", "per_agent_recall", "agent_handle", "query_count"
        ),
        _Breakdown("Hit decay bands", "hit_decay_bands", "band", "hit_count"),
    )

    @staticmethod
    def _render_insights(info: Mapping[str, Any]) -> str:
        # ``Any``: info is InsightsResponse.model_dump(), a heterogeneous JSON
        # tree (scalars at top level, lists of row dicts nested) -- not a
        # single schema this formatter narrows before rendering.
        """Render the insights payload as key-value lines plus row breakdowns."""
        enabled = "enabled" if info.get("telemetry_enabled") else "disabled"
        rate = float(info.get("empty_result_rate", 0.0)) * 100
        lines = [
            "▶  quarry insights",
            f"   Telemetry:        {enabled}",
            f"   Total queries:    {info.get('total_queries', 0)}",
            f"   Empty-result rate: {rate:.1f}%",
            f"   Latency p50/p95:  {info.get('p50_latency_ms', 0.0):.1f}ms / "
            f"{info.get('p95_latency_ms', 0.0):.1f}ms",
            f"   Memory/Knowledge: {info.get('memory_queries', 0)} / "
            f"{info.get('knowledge_queries', 0)}",
        ]
        for spec in SyncCli._BREAKDOWNS:
            lines.extend(SyncCli._rows(info, spec))
        return "\n".join(lines)

    @staticmethod
    def _rows(info: Mapping[str, Any], spec: _Breakdown) -> list[str]:
        """Return a titled ``label: count`` block for one insights breakdown."""
        rows: list[Mapping[str, Any]] = info.get(spec.key) or []
        if not rows:
            return []
        return [f"   {spec.title}:"] + [
            f"      {r[spec.label_field]}: {r[spec.count_field]}" for r in rows
        ]
