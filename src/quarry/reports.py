"""Text reports for ``quarry status`` and ``quarry insights``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from quarry.api.insights import InsightsResponse

_GIB = 1024**3


@final
class StatusReport:
    """Render one ``/status`` body as the key-value block ``quarry status`` prints."""

    __slots__ = ("_info",)

    _info: Mapping[str, Any]

    def __new__(cls, info: Mapping[str, Any]) -> Self:
        """Retain one daemon status response for CLI rendering."""
        self = super().__new__(cls)
        self._info = info
        return self

    def render(self) -> str:
        """Return the report text."""
        get = self._info.get
        lines = [
            "▶  quarry status",
            f"   Documents:      {get('document_count', 0)}",
            f"   Collections:    {get('collection_count', 0)}",
            f"   Chunks:         {get('chunk_count', 0):,}",
            f"   Directories:    {get('registered_directories', 0)}",
            f"   Database:       {get('database_path', '?')}",
            (
                f"   Table size:     {self._size('disk_bytes', 'on disk')}, "
                f"{self._size('live_bytes', 'live')}"
            ),
            f"   Free disk:      {self._size('free_disk_bytes', 'free', places=1)}",
            f"   Model:          {get('embedding_model', '?')}",
            f"   Provider:       {get('provider', '?')}",
        ]
        return "\n".join(lines)

    def _size(self, key: str, label: str, *, places: int = 2) -> str:
        size = self._info.get(key, 0)
        if size is None:
            return f"{label} unmeasured"
        return f"{size / _GIB:.{places}f} GiB {label}"


@dataclass(frozen=True, slots=True)
class _Breakdown:
    """One row group: title, a typed row accessor, and the two fields it prints.

    ``rows_of`` reads its rows straight off the typed ``InsightsResponse``, so a
    wire field rename fails type-checking here instead of silently rendering blank.
    """

    title: str
    rows_of: Callable[[InsightsResponse], Sequence[Mapping[str, object]]]
    label_field: str
    count_field: str

    def lines(self, info: InsightsResponse) -> list[str]:
        """Return a titled ``label: count`` block, or nothing when there are no rows."""
        rows = self.rows_of(info)
        if not rows:
            return []
        return [f"   {self.title}:"] + [
            f"      {r[self.label_field]}: {r[self.count_field]}" for r in rows
        ]


@final
class InsightsReport:
    """Render the same recall-telemetry aggregations for CLI and MCP callers."""

    __slots__ = ("_info",)

    _BREAKDOWNS: ClassVar[tuple[_Breakdown, ...]] = (
        _Breakdown(
            "Top empty queries",
            lambda info: info.top_empty_queries,
            "query_scrubbed",
            "count",
        ),
        _Breakdown(
            "Per-collection hits",
            lambda info: info.per_collection_hits,
            "collection",
            "hit_count",
        ),
        _Breakdown(
            "Per-agent recall",
            lambda info: info.per_agent_recall,
            "agent_handle",
            "query_count",
        ),
        _Breakdown(
            "Hit decay bands", lambda info: info.hit_decay_bands, "band", "hit_count"
        ),
    )

    _info: InsightsResponse

    def __new__(cls, info: InsightsResponse) -> Self:
        """Retain one recall-telemetry response for shared CLI and MCP rendering."""
        self = super().__new__(cls)
        self._info = info
        return self

    def render(self) -> str:
        """Return key-value lines plus the row breakdowns."""
        info = self._info
        enabled = "enabled" if info.telemetry_enabled else "disabled"
        lines = [
            "▶  quarry insights",
            f"   Telemetry:        {enabled}",
            f"   Total queries:    {info.total_queries}",
            f"   Empty-result rate: {info.empty_result_rate * 100:.1f}%",
            (
                f"   Latency p50/p95:  {info.p50_latency_ms:.1f}ms / "
                f"{info.p95_latency_ms:.1f}ms"
            ),
            f"   Memory/Knowledge: {info.memory_queries} / {info.knowledge_queries}",
        ]
        for breakdown in self._BREAKDOWNS:
            lines.extend(breakdown.lines(info))
        return "\n".join(lines)
