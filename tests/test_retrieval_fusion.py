"""RrfFusion: reciprocal rank fusion, deduplication, and temporal decay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from quarry.retrieval import RrfFusion


def _row(
    name: str,
    *,
    chunk: int = 0,
    memory_type: str = "",
    agent_handle: str = "",
    ts: object = "",
) -> dict[str, object]:
    return {
        "document_name": name,
        "chunk_index": chunk,
        "page_number": 1,
        "text": name,
        "memory_type": memory_type,
        "agent_handle": agent_handle,
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
        old = _row(
            "old",
            chunk=0,
            memory_type="fact",
            agent_handle="rmh",
            ts=now - timedelta(days=30),
        )
        recent = _row("recent", chunk=1, memory_type="fact", agent_handle="rmh", ts=now)

        no_decay = RrfFusion(rrf_k=60, decay_rate=0.0).fuse([old, recent], [], limit=2)
        assert no_decay[0].document_name == "old"  # rank order wins without decay

        decayed = RrfFusion(rrf_k=60, decay_rate=0.05).fuse([old, recent], [], limit=2)
        assert decayed[0].document_name == "recent"

    def test_empty_agent_handle_never_decays(self) -> None:
        """A chunk tagged with a decayable ``memory_type`` but no ``agent_handle``
        must be exempt from decay — knowledge is not memory.
        """
        now = datetime.now(tz=UTC)
        # Both rows carry ``memory_type="fact"``; only agent-owned rows should
        # decay. A bulk-ingested document may pick up the tag and must not lose
        # rank to a fresh row on that basis alone.
        old_knowledge = _row(
            "old", chunk=0, memory_type="fact", ts=now - timedelta(days=30)
        )
        recent_knowledge = _row("recent", chunk=1, memory_type="fact", ts=now)

        fused = RrfFusion(rrf_k=60, decay_rate=0.05).fuse(
            [old_knowledge, recent_knowledge], [], limit=2
        )
        assert fused[0].document_name == "old"

    def test_empty_agent_handle_and_no_type_never_decays(self) -> None:
        """A pure knowledge chunk (no handle, no type) never decays."""
        now = datetime.now(tz=UTC)
        old = _row("old", chunk=0, ts=now - timedelta(days=30))
        recent = _row("recent", chunk=1, ts=now)

        fused = RrfFusion(rrf_k=60, decay_rate=0.05).fuse([old, recent], [], limit=2)
        assert fused[0].document_name == "old"

    def test_lesson_boost_lifts_a_moderately_ranked_lesson(self) -> None:
        """A lesson-tagged row outranks an equal-or-better-ranked plain row.

        Per the design's derivation: r_l < boost*(60+r_p) - 60. At boost=1.5
        and r_p=0 (best-case plain competitor), r_l < 30 -- a lesson at rank
        20 in its own channel outranks a plain hit at rank 5.
        """
        lesson = _row("lesson-doc", memory_type="lesson")
        plain = _row("plain-doc", chunk=1)
        # lesson at rank 20, plain at rank 5 -- both from the vector channel.
        vec_results = (
            [_row(f"filler-{i}", chunk=i + 2) for i in range(5)]
            + [plain]
            + [_row(f"filler2-{i}", chunk=i + 10) for i in range(14)]
            + [lesson]
        )
        fused = RrfFusion(rrf_k=60, decay_rate=0.0, lesson_boost=1.5).fuse(
            vec_results, [], limit=len(vec_results)
        )
        names = [r.document_name for r in fused]
        assert names.index("lesson-doc") < names.index("plain-doc")

    def test_lesson_boost_is_a_noop_at_one(self) -> None:
        """boost=1.0 reproduces plain RRF rank order -- the neutral default."""
        lesson = _row("lesson-doc", memory_type="lesson", chunk=1)
        plain = _row("plain-doc", chunk=0)
        fused = RrfFusion(rrf_k=60, decay_rate=0.0, lesson_boost=1.0).fuse(
            [plain, lesson], [], limit=2
        )
        assert fused[0].document_name == "plain-doc"

    def test_lesson_boost_does_not_apply_to_other_memory_types(self) -> None:
        """The boost keys on memory_type=='lesson' exactly, not decayable types."""
        fact = _row("fact-doc", memory_type="fact", chunk=1)
        plain = _row("plain-doc", chunk=0)
        fused = RrfFusion(rrf_k=60, decay_rate=0.0, lesson_boost=1.5).fuse(
            [plain, fact], [], limit=2
        )
        assert fused[0].document_name == "plain-doc"

    def test_lesson_boost_composes_with_decay_never_decaying_lessons(self) -> None:
        """A lesson row is never decayed regardless of decay_rate.

        Lessons never carry an agent_handle (D1/D3), so the existing decay
        gate already exempts them -- confirmed here with both knobs active.
        """
        now = datetime.now(tz=UTC)
        old_lesson = _row(
            "old-lesson", memory_type="lesson", chunk=0, ts=now - timedelta(days=90)
        )
        recent_plain = _row("recent-plain", chunk=1, ts=now)
        fused = RrfFusion(rrf_k=60, decay_rate=0.05, lesson_boost=1.5).fuse(
            [old_lesson, recent_plain], [], limit=2
        )
        # The boosted, undecayed old lesson still outranks the equally-ranked
        # (rank 0 vs rank 1) recent plain row.
        assert fused[0].document_name == "old-lesson"

    def test_null_agent_handle_never_decays(self) -> None:
        """``agent_handle=None`` must be treated as absent, not the literal ``"None"``.

        LanceDB returns ``None`` for NULL columns; ``str(None) == "None"`` is
        truthy, so a naive check would decay knowledge chunks whose handle is
        NULL rather than empty-string.
        """
        now = datetime.now(tz=UTC)
        old_ts = now - timedelta(days=30)
        old = dict(_row("old", chunk=0, memory_type="fact", ts=old_ts))
        old["agent_handle"] = None
        recent = dict(_row("recent", chunk=1, memory_type="fact", ts=now))
        recent["agent_handle"] = None

        fused = RrfFusion(rrf_k=60, decay_rate=0.05).fuse([old, recent], [], limit=2)
        assert fused[0].document_name == "old"


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
