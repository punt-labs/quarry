"""RrfFusion: reciprocal rank fusion, deduplication, and temporal decay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from quarry.retrieval import RrfFusion


def _row(
    name: str, *, chunk: int = 0, memory_type: str = "", ts: object = ""
) -> dict[str, object]:
    return {
        "document_name": name,
        "chunk_index": chunk,
        "page_number": 1,
        "text": name,
        "memory_type": memory_type,
        "ingestion_timestamp": ts,
        "_distance": 0.1,
    }


class TestFuse:
    def test_row_in_both_channels_outranks_single_channel_row(self) -> None:
        """A row surfaced by vector AND FTS accumulates both RRF terms."""
        both = _row("both")
        vec_only = _row("vec", chunk=1)
        fused = RrfFusion(rrf_k=60, decay_rate=0.0).fuse(
            [both, vec_only], [both], limit=2
        )
        assert [r.document_name for r in fused] == ["both", "vec"]

    def test_dedup_collapses_identical_key(self) -> None:
        row = _row("dup")
        fused = RrfFusion(rrf_k=60, decay_rate=0.0).fuse([row], [row], limit=5)
        assert len(fused) == 1

    def test_limit_truncates(self) -> None:
        rows = [_row(f"d{i}", chunk=i) for i in range(5)]
        fused = RrfFusion(rrf_k=60, decay_rate=0.0).fuse(rows, [], limit=2)
        assert len(fused) == 2

    def test_empty_channels_yield_empty(self) -> None:
        assert RrfFusion(rrf_k=60, decay_rate=0.0).fuse([], [], limit=5) == []

    def test_decay_lifts_recent_decayable_row(self) -> None:
        """With decay, a recent fact overtakes an older, higher-ranked fact."""
        now = datetime.now(tz=UTC)
        old = _row("old", chunk=0, memory_type="fact", ts=now - timedelta(days=30))
        recent = _row("recent", chunk=1, memory_type="fact", ts=now)

        no_decay = RrfFusion(rrf_k=60, decay_rate=0.0).fuse([old, recent], [], limit=2)
        assert no_decay[0].document_name == "old"  # rank order wins without decay

        decayed = RrfFusion(rrf_k=60, decay_rate=0.05).fuse([old, recent], [], limit=2)
        assert decayed[0].document_name == "recent"


class TestTemporalWeight:
    def test_no_decay_returns_one(self) -> None:
        ts = datetime.now(tz=UTC)
        assert RrfFusion.temporal_weight(ts, ts.timestamp(), 0.0) == 1.0

    def test_recent_weighted_higher_than_old(self) -> None:
        now = datetime.now(tz=UTC)
        w_recent = RrfFusion.temporal_weight(
            now - timedelta(hours=1), now.timestamp(), 0.01
        )
        w_old = RrfFusion.temporal_weight(
            now - timedelta(hours=100), now.timestamp(), 0.01
        )
        assert w_recent > w_old

    def test_string_timestamp_parsed(self) -> None:
        now = datetime.now(tz=UTC)
        ts_str = (now - timedelta(hours=24)).isoformat()
        # exp(-0.01 * 24) ~= 0.787
        assert 0.75 < RrfFusion.temporal_weight(ts_str, now.timestamp(), 0.01) < 0.80

    def test_unparsable_timestamp_returns_one(self) -> None:
        now = datetime.now(tz=UTC)
        assert RrfFusion.temporal_weight("not-a-date", now.timestamp(), 0.01) == 1.0

    def test_naive_datetime_treated_as_utc(self) -> None:
        now = datetime.now(tz=UTC)
        naive = (now - timedelta(hours=1)).replace(tzinfo=None)
        assert RrfFusion.temporal_weight(naive, now.timestamp(), 0.01) == pytest.approx(
            RrfFusion.temporal_weight(now - timedelta(hours=1), now.timestamp(), 0.01)
        )
