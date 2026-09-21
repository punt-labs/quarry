"""Tests for the query-log SQLite store: schema, record, prune, insights."""

from __future__ import annotations

import sqlite3
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

import pytest

import quarry.query_log_schema as query_log_schema
from quarry.config import Settings
from quarry.query_log import QueryLog, get_query_log
from quarry.query_log_schema import QueryLogSchema
from quarry.query_log_types import QueryEvent, QueryHit


def _mode(path: Path) -> int:
    """Return *path*'s permission bits (the low 12 bits of ``st_mode``)."""
    return stat.S_IMODE(path.stat().st_mode)


_RETENTION_DAYS = 90


def _event(
    *,
    ts: str = "",
    surface: str = "cli",
    agent_handle: str = "",
    collection: str = "default",
    result_count: int = 1,
    query_scrubbed: str = "hello world",
    latency_ms: float = 12.5,
) -> QueryEvent:
    return QueryEvent(
        ts=ts or datetime.now(UTC).isoformat(),
        surface=surface,
        agent_handle=agent_handle,
        collection=collection,
        filters_json="{}",
        limit_n=10,
        latency_ms=latency_ms,
        result_count=result_count,
        query_scrubbed=query_scrubbed,
        query_len=len(query_scrubbed),
    )


def _hit(
    *, rank: int = 1, document_name: str = "doc.md", collection: str = "default"
) -> QueryHit:
    return QueryHit(
        rank=rank,
        document_name=document_name,
        collection=collection,
        chunk_index=0,
        score=0.9,
        hit_agent_handle="",
        memory_type="",
    )


class TestOpenQueryLog:
    def test_creates_tables(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        conn = sqlite3.connect(str(tmp_path / "telemetry.db"))
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        conn.close()
        log.close()
        assert "query_events" in tables
        assert "query_hits" in tables

    def test_idempotent_across_reopen(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telemetry.db"
        QueryLog(db_path).close()
        log2 = QueryLog(db_path)  # must not raise on a second open
        assert log2.insights.aggregate().total_queries == 0
        log2.close()

    def test_wal_mode(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        mode = log._conn.execute("PRAGMA journal_mode").fetchone()
        log.close()
        assert mode is not None
        assert mode[0] == "wal"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        db_path = tmp_path / "nested" / "dir" / "telemetry.db"
        log = QueryLog(db_path)
        assert db_path.exists()
        log.close()


class TestFilePermissions:
    def test_directory_is_0700(self, tmp_path: Path) -> None:
        db_path = tmp_path / "sub" / "telemetry.db"
        log = QueryLog(db_path)
        log.close()
        assert _mode(db_path.parent) == 0o700

    def test_db_file_is_0600(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telemetry.db"
        log = QueryLog(db_path)
        log.close()
        assert _mode(db_path) == 0o600

    def test_tightens_an_already_loose_directory(self, tmp_path: Path) -> None:
        directory = tmp_path / "sub"
        directory.mkdir(mode=0o755)
        db_path = directory / "telemetry.db"
        log = QueryLog(db_path)
        log.close()
        assert _mode(directory) == 0o700

    def test_tightens_an_already_loose_db_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telemetry.db"
        db_path.touch(mode=0o644)
        db_path.chmod(0o644)  # touch()'s mode is umask-masked; force it loose
        log = QueryLog(db_path)
        log.close()
        assert _mode(db_path) == 0o600


class TestSchemaMigration:
    def test_initialize_twice_is_a_noop(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        schema = QueryLogSchema(conn)
        schema.initialize()
        schema.initialize()  # must not raise
        conn.close()

    def test_migrate_twice_is_a_noop(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        schema = QueryLogSchema(conn)
        schema.initialize()
        schema.migrate()
        schema.migrate()  # must not raise, no columns added twice
        conn.close()

    def test_migrate_adds_a_missing_column_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_MIGRATIONS`` is empty in v1 -- feed a real spec so this test is
        non-vacuous (an empty dict's ``migrate()`` iterates nothing)."""
        monkeypatch.setattr(
            query_log_schema,
            "_MIGRATIONS",
            {"query_events": {"note": "TEXT NOT NULL DEFAULT ''"}},
        )
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        schema = QueryLogSchema(conn)
        schema.initialize()
        schema.migrate()
        columns_sql = "PRAGMA table_info(query_events)"
        first_pass = {row[1] for row in conn.execute(columns_sql)}
        schema.migrate()  # second pass: must not raise or add "note" again
        second_pass = {row[1] for row in conn.execute(columns_sql)}
        conn.close()
        assert "note" in first_pass
        assert second_pass == first_pass


class TestRecord:
    def test_record_writes_one_event_row(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        log.record(_event(), [])
        row = log._conn.execute("SELECT query_scrubbed FROM query_events").fetchone()
        log.close()
        assert row is not None
        assert row[0] == "hello world"

    def test_record_writes_n_hit_rows(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        hits = [_hit(rank=1, document_name="a.md"), _hit(rank=2, document_name="b.md")]
        log.record(_event(result_count=2), hits)
        rows = log._conn.execute(
            "SELECT document_name FROM query_hits ORDER BY rank"
        ).fetchall()
        log.close()
        assert [r[0] for r in rows] == ["a.md", "b.md"]

    def test_record_with_zero_hits_writes_no_hit_rows(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        log.record(_event(result_count=0), [])
        count = log._conn.execute("SELECT COUNT(*) FROM query_hits").fetchone()[0]
        log.close()
        assert count == 0

    def test_hit_rows_reference_their_event(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        log.record(_event(), [_hit()])
        event_id = log._conn.execute("SELECT id FROM query_events").fetchone()[0]
        hit_event_id = log._conn.execute(
            "SELECT query_event_id FROM query_hits"
        ).fetchone()[0]
        log.close()
        assert hit_event_id == event_id


class TestPrune:
    def test_prune_deletes_old_events_and_hits(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        stale_ts = (datetime.now(UTC) - timedelta(days=_RETENTION_DAYS + 1)).isoformat()
        log.record(_event(ts=stale_ts), [_hit()])
        deleted = log.prune(_RETENTION_DAYS)
        counts = (
            log._conn.execute("SELECT COUNT(*) FROM query_events").fetchone()[0],
            log._conn.execute("SELECT COUNT(*) FROM query_hits").fetchone()[0],
        )
        log.close()
        assert deleted == 1
        assert counts == (0, 0)

    def test_prune_keeps_recent_events(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        log.record(_event(), [_hit()])
        deleted = log.prune(_RETENTION_DAYS)
        total = log.insights.aggregate().total_queries
        log.close()
        assert deleted == 0
        assert total == 1

    def test_prune_on_empty_store_returns_zero(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        deleted = log.prune(_RETENTION_DAYS)
        log.close()
        assert deleted == 0


class TestInsightsAggregate:
    def test_empty_store_reports_zeros(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        agg = log.insights.aggregate()
        log.close()
        assert agg.total_queries == 0
        assert agg.empty_result_rate == 0.0
        assert agg.p50_latency_ms == 0.0
        assert agg.p95_latency_ms == 0.0

    def test_total_queries_counts_every_event(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        log.record(_event(), [])
        log.record(_event(), [])
        total = log.insights.aggregate().total_queries
        log.close()
        assert total == 2

    def test_empty_result_rate(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        log.record(_event(result_count=0), [])
        log.record(_event(result_count=1), [_hit()])
        rate = log.insights.aggregate().empty_result_rate
        log.close()
        assert rate == pytest.approx(0.5)

    def test_top_empty_queries_only_counts_empty_events(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        log.record(_event(result_count=0, query_scrubbed="nada"), [])
        log.record(_event(result_count=1, query_scrubbed="found it"), [_hit()])
        top = log.insights.aggregate().top_empty_queries
        log.close()
        assert [row["label"] for row in top] == ["nada"]

    def test_per_collection_hits_groups_by_collection(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        log.record(
            _event(collection="alpha", result_count=2),
            [_hit(collection="alpha"), _hit(collection="alpha", rank=2)],
        )
        rows = {
            r["label"]: r["count"] for r in log.insights.aggregate().per_collection_hits
        }
        log.close()
        assert rows == {"alpha": 2}

    def test_per_agent_recall_excludes_blank_agent_handle(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        log.record(_event(agent_handle="rmh"), [])
        log.record(_event(agent_handle=""), [])
        rows = {
            r["label"]: r["count"] for r in log.insights.aggregate().per_agent_recall
        }
        log.close()
        assert rows == {"rmh": 1}

    def test_memory_vs_knowledge_split(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        log.record(_event(agent_handle="rmh"), [])
        log.record(_event(agent_handle=""), [])
        log.record(_event(agent_handle=""), [])
        agg = log.insights.aggregate()
        log.close()
        assert agg.memory_queries == 1
        assert agg.knowledge_queries == 2

    def test_latency_percentiles_over_a_known_series(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        for latency in (10.0, 20.0, 30.0, 40.0, 50.0):
            log.record(_event(latency_ms=latency), [])
        agg = log.insights.aggregate()
        log.close()
        # Nearest-rank over 5 sorted values: p50 index=2 (30.0), p95 index=4 (50.0).
        assert agg.p50_latency_ms == pytest.approx(30.0)
        assert agg.p95_latency_ms == pytest.approx(50.0)

    def test_latency_percentiles_over_an_even_sized_series(
        self, tmp_path: Path
    ) -> None:
        """``ceil(pct * n) - 1`` over an even ``n`` (MUST-FIX): plain
        ``int(pct * n)`` is off by one whenever ``pct * n`` lands on an
        integer -- p50 of 4 sorted values must read index 1 (20.0), not
        index 2 (30.0)."""
        log = QueryLog(tmp_path / "telemetry.db")
        for latency in (10.0, 20.0, 30.0, 40.0):
            log.record(_event(latency_ms=latency), [])
        agg = log.insights.aggregate()
        log.close()
        assert agg.p50_latency_ms == pytest.approx(20.0)
        assert agg.p95_latency_ms == pytest.approx(40.0)

    def test_latency_percentile_is_bounded_over_a_large_series(
        self, tmp_path: Path
    ) -> None:
        """The percentile read is a single bounded row fetch, not a full-table
        load into Python -- correctness over a series too large to eyeball
        stands in for that bound (the SQL itself is asserted by inspection)."""
        log = QueryLog(tmp_path / "telemetry.db")
        for i in range(1, 1001):
            log.record(_event(latency_ms=float(i)), [])
        agg = log.insights.aggregate()
        log.close()
        assert agg.p50_latency_ms == pytest.approx(500.0)
        assert agg.p95_latency_ms == pytest.approx(950.0)


class TestRecentHits:
    def test_recent_hits_returns_document_identity(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        log.record(_event(result_count=1), [_hit(document_name="report.pdf")])
        refs = log.insights.recent_hits()
        log.close()
        assert refs == [{"document_name": "report.pdf", "collection": "default"}]

    def test_recent_hits_respects_limit(self, tmp_path: Path) -> None:
        log = QueryLog(tmp_path / "telemetry.db")
        for i in range(3):
            log.record(_event(result_count=1), [_hit(document_name=f"doc-{i}.md")])
        refs = log.insights.recent_hits(limit=1)
        log.close()
        assert len(refs) == 1


class TestGetQueryLog:
    def test_returns_the_same_instance_for_the_same_path(self, tmp_path: Path) -> None:
        get_query_log.cache_clear()
        path = tmp_path / "telemetry.db"
        first = get_query_log(path)
        second = get_query_log(path)
        assert first is second
        first.close()
        get_query_log.cache_clear()

    def test_prunes_on_first_construction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        get_query_log.cache_clear()
        db_path = tmp_path / "telemetry.db"
        seed = QueryLog(db_path)
        stale_ts = (datetime.now(UTC) - timedelta(days=200)).isoformat()
        seed.record(_event(ts=stale_ts), [])
        seed.close()

        monkeypatch.setattr(
            Settings,
            "load",
            classmethod(lambda cls: Settings(telemetry_retention_days=1)),
        )
        log = get_query_log(db_path)
        total = log.insights.aggregate().total_queries
        log.close()
        get_query_log.cache_clear()
        assert total == 0

    def test_second_record_after_cadence_elapses_prunes_the_stale_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``QueryLog.prune`` must not run only once at construction (MUST-FIX):
        a long-lived daemon's later ``record()`` calls must re-prune once the
        configured cadence has elapsed, not leave rows past
        ``telemetry_retention_days`` forever."""
        get_query_log.cache_clear()
        monkeypatch.setattr(
            Settings,
            "load",
            classmethod(
                lambda cls: Settings(
                    telemetry_retention_days=1, telemetry_prune_cadence_s=1.0
                )
            ),
        )
        # One monotonic() call each for configure_retention() and the two
        # record() calls' _maybe_prune() checks: cadence not yet elapsed after
        # the first record, elapsed (1000s later) by the second.
        ticks = iter([0.0, 0.0, 1000.0])
        monkeypatch.setattr(
            "quarry.query_log_prune.time.monotonic", lambda: next(ticks)
        )

        log = get_query_log(tmp_path / "telemetry.db")
        stale_ts = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        log.record(_event(ts=stale_ts), [])  # cadence not elapsed: no prune yet
        log.record(_event(), [])  # cadence elapsed: prunes the stale row above
        total = log.insights.aggregate().total_queries
        log.close()
        get_query_log.cache_clear()
        assert total == 1

    def test_prune_failure_closes_the_connection_and_reraises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A prune failure must not leak the connection @cache discards (MUST-FIX)."""
        get_query_log.cache_clear()
        closed: list[QueryLog] = []
        original_close = QueryLog.close

        def _tracking_close(self: QueryLog) -> None:
            closed.append(self)
            original_close(self)

        def _raising_prune(self: QueryLog, retention_days: int) -> int:
            raise RuntimeError("prune boom")

        monkeypatch.setattr(QueryLog, "close", _tracking_close)
        monkeypatch.setattr(QueryLog, "prune", _raising_prune)

        with pytest.raises(RuntimeError, match="prune boom"):
            get_query_log(tmp_path / "telemetry.db")
        get_query_log.cache_clear()

        assert len(closed) == 1


class TestPruneBoundary:
    def test_event_exactly_at_the_cutoff_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``WHERE ts < cutoff`` is exclusive -- an event dated exactly at the
        retention boundary survives one more prune cycle."""

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> Self:
                return cls(2026, 1, 1, tzinfo=UTC)

        monkeypatch.setattr("quarry.query_log.datetime", _FixedDatetime)
        fixed_now = _FixedDatetime.now()
        cutoff_ts = (fixed_now - timedelta(days=_RETENTION_DAYS)).isoformat()

        log = QueryLog(tmp_path / "telemetry.db")
        log.record(_event(ts=cutoff_ts), [_hit()])
        deleted = log.prune(_RETENTION_DAYS)
        total = log.insights.aggregate().total_queries
        log.close()

        assert deleted == 0
        assert total == 1


class TestConcurrentWriters:
    def test_concurrent_records_never_split_an_event_from_its_hits(
        self, tmp_path: Path
    ) -> None:
        """Many threads racing ``record()`` on one connection (MUST-FIX): every
        ``query_events`` row must end with exactly the hit count it was given,
        never an interleaved/partial count from another thread's write."""
        log = QueryLog(tmp_path / "telemetry.db")
        thread_count = 8
        records_per_thread = 25

        def _worker(thread_id: int) -> None:
            for i in range(records_per_thread):
                hit_count = (thread_id + i) % 5
                hits = [_hit(rank=r + 1) for r in range(hit_count)]
                log.record(_event(result_count=hit_count), hits)

        threads = [
            threading.Thread(target=_worker, args=(t,)) for t in range(thread_count)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = log._conn.execute(
            "SELECT e.result_count, COUNT(h.query_event_id) FROM query_events e "
            "LEFT JOIN query_hits h ON h.query_event_id = e.id GROUP BY e.id"
        ).fetchall()
        total_events = log._conn.execute(
            "SELECT COUNT(*) FROM query_events"
        ).fetchone()[0]
        log.close()

        assert total_events == thread_count * records_per_thread
        for result_count, actual_hits in rows:
            assert actual_hits == result_count


@pytest.mark.resource
class TestNoDescriptorLeak:
    def test_many_records_do_not_leak_descriptors(self, tmp_path: Path) -> None:
        """Hundreds of ``record()`` calls on one connection must not leak fds."""

        def _open_fd_count() -> int:
            return sum(1 for _ in Path("/proc/self/fd").iterdir())

        if not Path("/proc/self/fd").is_dir():
            pytest.skip("no /proc/self/fd on this platform")

        log = QueryLog(tmp_path / "telemetry.db")
        before = _open_fd_count()
        for i in range(500):
            log.record(_event(query_scrubbed=f"query {i}"), [_hit()])
        after = _open_fd_count()
        log.close()
        assert after - before <= 2, (
            f"fd count grew {before} -> {after} over 500 records"
        )
