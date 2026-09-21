"""Local SQLite store for recall telemetry: query events and their ranked hits.

Mirrors :class:`~quarry.sync_registry.SyncRegistry`'s connection lifecycle (WAL,
a busy timeout, ``check_same_thread=False``) so the daemon holds one
daemon-lifetime connection per database, built lazily via :func:`get_query_log`
and cached for the process — the same "one resident connection" shape as
``QuarryResources.database``, without adding a new field to ``DaemonContext``.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Self, final

from quarry.config import Settings
from quarry.query_log_insights import QueryLogInsights
from quarry.query_log_prune import PruneSchedule
from quarry.query_log_schema import QueryLogSchema
from quarry.query_log_types import QueryEvent, QueryHit
from quarry.telemetry_perms import TelemetryPathGuard


@final
class QueryLog:
    """Own the query-log SQLite connection: record, prune, and expose insights.

    A daemon-lifetime connection built lazily via :func:`get_query_log`. Every
    write is a boundary I/O side effect the search route swallows on failure
    (PY-EH boundary I/O) -- this store's job is to never be the reason a search
    fails, so its own methods stay simple and let sqlite3 errors propagate to
    that one guarded call site rather than swallowing them here.
    """

    _conn: sqlite3.Connection
    _lock: threading.Lock
    _insights: QueryLogInsights
    _prune_schedule: PruneSchedule

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        TelemetryPathGuard(path).secure()
        # check_same_thread=False: background ingest/task threads share the
        # daemon's one connection with the request-handling thread, matching
        # SyncRegistry's rationale. The connection itself serializes nothing --
        # ``_lock`` is what makes concurrent record()/prune()/insights reads
        # from the threadpool safe against interleaving (DES-056 round 2).
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        try:
            self._ensure_schema()
        except Exception:
            self._conn.close()
            raise
        self._insights = QueryLogInsights(self._conn, self._lock)
        # Unconfigured by default -- a bare ``QueryLog(path)`` (every test in
        # this module, plus ``prune()``'s own direct callers) never re-prunes
        # itself as a record() side effect; only :func:`get_query_log`, via
        # :meth:`configure_retention`, opts a daemon-lifetime instance in.
        self._prune_schedule = PruneSchedule()
        return self

    @property
    def insights(self) -> QueryLogInsights:
        """Return the read-only aggregation surface sharing this connection."""
        return self._insights

    def configure_retention(self, retention_days: int, prune_cadence_s: float) -> None:
        """Enable opportunistic pruning from :meth:`record`, at *prune_cadence_s*.

        Called once by :func:`get_query_log` right after its initial prune --
        see :meth:`__new__`'s comment for why a bare instance stays inert
        without this call.
        """
        self._prune_schedule.configure(retention_days, prune_cadence_s)

    def _ensure_schema(self) -> None:
        """Set connection pragmas, create tables, and apply migrations."""
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Wait up to 5 s for a contended write lock, matching SyncRegistry.
        self._conn.execute("PRAGMA busy_timeout=5000")
        schema = QueryLogSchema(self._conn)
        schema.initialize()
        schema.migrate()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def record(self, event: QueryEvent, hits: Sequence[QueryHit]) -> None:
        """Insert one ``query_events`` row plus one ``query_hits`` row per hit.

        The lock plus ``with self._conn:`` together make this atomic against
        both concurrent threads (no interleaved event/hit writes) and partial
        failure (``sqlite3.Connection`` used as a context manager commits on
        clean exit and rolls back -- never leaving an event committed without
        its hits -- on any exception, which then propagates to the caller).
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO query_events "
                "(ts, surface, agent_handle, collection, filters_json, limit_n, "
                "latency_ms, result_count, query_scrubbed, query_len) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event["ts"],
                    event["surface"],
                    event["agent_handle"],
                    event["collection"],
                    event["filters_json"],
                    event["limit_n"],
                    event["latency_ms"],
                    event["result_count"],
                    event["query_scrubbed"],
                    event["query_len"],
                ),
            )
            event_id = cursor.lastrowid
            if hits:
                self._conn.executemany(
                    "INSERT INTO query_hits "
                    "(query_event_id, rank, document_name, collection, chunk_index, "
                    "score, hit_agent_handle, memory_type) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            event_id,
                            hit["rank"],
                            hit["document_name"],
                            hit["collection"],
                            hit["chunk_index"],
                            hit["score"],
                            hit["hit_agent_handle"],
                            hit["memory_type"],
                        )
                        for hit in hits
                    ],
                )
        self._maybe_prune()

    def _maybe_prune(self) -> None:
        """Prune expired rows if :attr:`_prune_schedule` says one is due.

        A daemon that stays up past ``telemetry_retention_days`` must keep
        pruning, not just once at :func:`get_query_log`'s first construction --
        :meth:`record` is the one steady heartbeat a long-lived daemon gets, so
        it is where the second (and every later) prune is triggered from. The
        schedule check runs under a short-lived lock acquisition of its own,
        separate from :meth:`prune`'s -- :meth:`record`'s own lock is already
        released by the time this runs, and ``threading.Lock`` is not
        reentrant, so calling :meth:`prune` while still holding it would
        deadlock.
        """
        with self._lock:
            retention_days = self._prune_schedule.due()
        if retention_days is not None:
            self.prune(retention_days)

    def prune(self, retention_days: int) -> int:
        """Delete events and hits older than *retention_days*; return the count.

        See :meth:`record` for why the lock plus ``with self._conn:`` together
        give atomicity against concurrent threads and partial failure.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self._lock, self._conn:
            stale_ids = [
                row[0]
                for row in self._conn.execute(
                    "SELECT id FROM query_events WHERE ts < ?", (cutoff,)
                )
            ]
            if not stale_ids:
                return 0
            # One row per statement rather than a dynamic IN-clause: a fixed
            # parameterized statement needs no f-string SQL at all, so there is
            # nothing for a query-count-scale prune batch to lose by avoiding it.
            rows = [(i,) for i in stale_ids]
            self._conn.executemany(
                "DELETE FROM query_hits WHERE query_event_id = ?", rows
            )
            self._conn.executemany("DELETE FROM query_events WHERE id = ?", rows)
            return len(stale_ids)


@cache
def get_query_log(path: Path) -> QueryLog:
    """Return the process-cached, daemon-lifetime :class:`QueryLog` for *path*.

    One entry per resolved path, so every route handler in the process shares the
    same connection without a new field on ``DaemonContext`` -- the same "build
    once, cache for the process" shape as ``ProviderSelection.display_cached()``.
    Pruned once here, at first construction, which approximates "on daemon
    start" for the process that first touches telemetry (search or insights).
    :meth:`QueryLog.configure_retention` then arms :meth:`~QueryLog.record`'s
    opportunistic re-prune, so a daemon that stays up past
    ``telemetry_retention_days`` keeps enforcing it rather than pruning only
    this once.

    ``@cache`` does not memoize a raised call, so a prune failure must close the
    connection before re-raising -- otherwise every subsequent search or
    insights read opens (and leaks) a fresh, never-closed connection to the
    same file, exhausting file descriptors under sustained failure.
    """
    settings = Settings.load()
    log = QueryLog(path)
    try:
        log.prune(settings.telemetry_retention_days)
    except Exception:
        log.close()
        raise
    log.configure_retention(
        settings.telemetry_retention_days, settings.telemetry_prune_cadence_s
    )
    return log
