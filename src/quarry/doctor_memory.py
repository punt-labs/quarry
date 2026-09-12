"""Memory-corpus doctor checks: per-handle counts and identity activation.

Reports the shape of the agent-memory corpus (rows per ``agent_handle``, per
``memory_type``, per ``collection``) and warns when an ethos identity is active
in the current repo but has zero rows in the database — the "my ethos config
resolves but PreCompact never fired" failure mode.
"""

from __future__ import annotations

from collections import Counter
from functools import partial
from pathlib import Path
from typing import Self, final

from quarry.ethos_handle import EthosConfig
from quarry.results import CheckResult

# The four memory_type values RrfFusion decays; the same set names the types
# the corpus check counts (see quarry.retrieval.fusion._DECAYABLE_TYPES).
_MEMORY_TYPES: frozenset[str] = frozenset(
    {"fact", "observation", "opinion", "procedure"}
)
# A quarry-learn lesson row always has an empty agent_handle by design (it is
# project-scoped, not agent-scoped), so it would otherwise be invisible to
# both the handle and type tallies below -- counted separately here.
_LESSON_TYPE = "lesson"


@final
class _CorpusTally:
    """Accumulate per-handle, per-type, per-collection, and lesson counts.

    One row at a time via :meth:`add`, so the branching that decides which
    counters a row affects lives in one place instead of a four-way tuple a
    caller has to destructure.
    """

    __slots__ = ("_collections", "_handles", "_lessons", "_types")

    _handles: Counter[str]
    _types: Counter[str]
    _collections: Counter[str]
    _lessons: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._handles = Counter()
        self._types = Counter()
        self._collections = Counter()
        self._lessons = 0
        return self

    def add(self, row: dict[str, object]) -> None:
        """Fold one chunk row's handle, type, collection, and lesson tag in."""
        handle = str(row.get("agent_handle") or "")
        memory_type = str(row.get("memory_type") or "")
        collection = str(row.get("collection") or "")
        if handle:
            self._add_owned(handle, memory_type)
        if memory_type == _LESSON_TYPE:
            self._lessons += 1
        if collection:
            self._collections[collection] += 1

    def _add_owned(self, handle: str, memory_type: str) -> None:
        """Tally an agent-owned row's handle and (if decayable) its type."""
        self._handles[handle] += 1
        if memory_type in _MEMORY_TYPES:
            self._types[memory_type] += 1

    @property
    def handles(self) -> Counter[str]:
        return self._handles

    @property
    def types(self) -> Counter[str]:
        return self._types

    @property
    def collections(self) -> Counter[str]:
        return self._collections

    @property
    def lessons(self) -> int:
        return self._lessons


@final
class MemoryDiagnostics:
    """Doctor checks for the agent-memory corpus and identity activation."""

    __slots__ = ()

    @staticmethod
    def corpus(db_path: Path) -> CheckResult:
        """Report per-handle, per-type, and per-collection row counts.

        Informational only (``required=False``). Rows with empty ``agent_handle``
        are excluded from the handle and type tallies but every collection is
        reported so the operator sees the split between ``memory-*`` and
        capture buckets.
        """
        result = partial(CheckResult, name="Memory corpus", required=False)
        if not db_path.exists():
            return result(passed=True, message="no data yet")
        try:
            summary = MemoryDiagnostics._summarize(db_path)
        except (RuntimeError, OSError, ValueError) as exc:
            return result(passed=False, message=f"check failed: {exc}")
        return result(passed=True, message=summary)

    @staticmethod
    def identity_active(cwd: str, db_path: Path) -> CheckResult:
        """Warn when the resident ethos handle exists but owns zero memory rows.

        Warning-only (``required=False``). Passes silently when no identity is
        configured for *cwd* or when the corpus holds no memory rows (knowledge
        chunks with empty ``agent_handle`` do not count); warns when the handle
        is set and other agents own memory rows but this handle owns none.
        """
        result = partial(CheckResult, name="Memory identity", required=False)
        handle = EthosConfig.agent_handle_at(cwd)
        if not handle:
            return result(passed=True, message="no ethos identity active")
        if not db_path.exists():
            return result(passed=True, message=f"identity '{handle}' active")
        try:
            rows_for_handle, total_rows = MemoryDiagnostics._handle_counts(
                db_path, handle
            )
        except (RuntimeError, OSError, ValueError) as exc:
            return result(passed=False, message=f"check failed: {exc}")
        if total_rows == 0:
            return result(passed=True, message=f"identity '{handle}' active")
        if rows_for_handle == 0:
            return result(
                passed=False,
                message=(
                    f"identity '{handle}' active in this repo but has zero "
                    "memory rows; check that ethos config resolves and "
                    "PreCompact fires"
                ),
            )
        return result(
            passed=True,
            message=f"identity '{handle}' has {rows_for_handle} memory rows",
        )

    @staticmethod
    def _summarize(db_path: Path) -> str:
        """Return the one-line summary printed as the ``corpus`` message."""
        rows = MemoryDiagnostics._scan_columns(
            db_path, ["agent_handle", "memory_type", "collection"]
        )
        if not rows:
            return "no chunks yet"
        tally = MemoryDiagnostics._tally(rows)
        parts = [
            MemoryDiagnostics._render(label, counts)
            for label, counts in (
                ("memory", tally.handles),
                ("types", tally.types),
                ("collections", tally.collections),
            )
        ]
        if tally.lessons:
            parts.append(f"lessons={tally.lessons}")
        return "; ".join(part for part in parts if part) or "no agent memory yet"

    @staticmethod
    def _tally(rows: list[dict[str, object]]) -> _CorpusTally:
        """Return the handle/type/collection/lesson tally over *rows*.

        Handle and type tallies count agent-owned rows only; a knowledge chunk
        (empty handle) may carry a ``memory_type`` tag from ingestion but is
        not a memory row and must not inflate the type count. Collections are
        tallied for every non-empty value so the operator sees the full split.
        Lessons count ``memory_type == "lesson"`` rows independently of
        ``agent_handle`` -- a lesson's handle is always empty by design, so
        gating it on ``handle`` would make every lesson invisible here.
        """
        tally = _CorpusTally()
        for row in rows:
            tally.add(row)
        return tally

    @staticmethod
    def _render(label: str, counts: Counter[str]) -> str:
        """Render one group as ``label: k1=v1 k2=v2`` in descending count order."""
        if not counts:
            return ""
        return f"{label}: " + " ".join(f"{k}={v}" for k, v in counts.most_common())

    @staticmethod
    def _handle_counts(db_path: Path, handle: str) -> tuple[int, int]:
        """Return ``(rows_for_handle, total_memory_rows)`` for identity_active.

        ``total_memory_rows`` counts only rows with a non-empty ``agent_handle``
        — knowledge chunks are corpus, not memory, and must not falsify the
        "corpus has memory for someone else" signal.
        """
        rows = MemoryDiagnostics._scan_columns(db_path, ["agent_handle"])
        handles = [str(r.get("agent_handle") or "") for r in rows]
        total = sum(1 for h in handles if h)
        matches = sum(1 for h in handles if h == handle)
        return (matches, total)

    @staticmethod
    def _scan_columns(db_path: Path, columns: list[str]) -> list[dict[str, object]]:
        """Scan the chunks table for *columns*, returning ``[]`` when empty.

        The single point that opens the LanceDB facade — both checks funnel
        through here so the "no table yet" signal is a single empty list, not
        two independent conditionals. The import stays lazy so this
        client-reachable module never pulls the engine onto the hot path
        (see the `.importlinter` exception for `quarry.doctor_memory`).
        """
        from quarry.db.facade import Database  # noqa: PLC0415
        from quarry.db.schema import TABLE_NAME  # noqa: PLC0415

        database = Database.connect(db_path)
        if TABLE_NAME not in database.db.list_tables().tables:
            return []
        table = database.db.open_table(TABLE_NAME)
        return list(table.search().limit(1_000_000).select(columns).to_list())
