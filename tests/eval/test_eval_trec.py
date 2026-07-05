"""Unit tests for TREC run/qrels I/O, subsetting, and round-tripping."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tools.eval.trec import Qrels, TrecRun

if TYPE_CHECKING:
    from pathlib import Path


def test_run_write_has_six_trec_columns(tmp_path: Path) -> None:
    run = TrecRun({"q1": [("d#doc", 1.0), ("e#doc", 0.5)]}, "tag1")
    path = tmp_path / "run.trec"
    run.write(path)
    first = path.read_text(encoding="utf-8").splitlines()[0].split()
    assert first == ["q1", "Q0", "d#doc", "1", "1.000000", "tag1"]


def test_run_round_trip_preserves_rank_order(tmp_path: Path) -> None:
    run = TrecRun({"q1": [("a#doc", 0.9), ("b#doc", 0.3), ("c#doc", 0.1)]}, "t")
    path = tmp_path / "run.trec"
    run.write(path)
    loaded = TrecRun.from_path(path)
    assert [d for d, _ in loaded.ranking("q1")] == ["a#doc", "b#doc", "c#doc"]
    assert loaded.tag == "t"


def test_run_subset_keeps_only_wanted_queries() -> None:
    run = TrecRun({"q1": [("a#doc", 1.0)], "q2": [("b#doc", 1.0)]}, "t")
    assert run.subset(["q1"]).query_ids == ["q1"]


def test_qrels_write_has_four_trec_columns(tmp_path: Path) -> None:
    qrels = Qrels({"q1": {"a#doc": 1}})
    path = tmp_path / "qrels.trec"
    qrels.write(path)
    assert path.read_text(encoding="utf-8").splitlines()[0].split() == [
        "q1",
        "0",
        "a#doc",
        "1",
    ]


def test_qrels_round_trip(tmp_path: Path) -> None:
    qrels = Qrels({"q1": {"a#doc": 1}, "q2": {"b#doc": 1, "c#doc": 0}})
    path = tmp_path / "qrels.trec"
    qrels.write(path)
    loaded = Qrels.from_path(path)
    assert loaded.relevant_docids("q1") == {"a#doc"}
    assert loaded.relevant_docids("q2") == {"b#doc"}
    assert loaded.judged_docids("q2") == {"b#doc", "c#doc"}


def test_qrels_subset() -> None:
    qrels = Qrels({"q1": {"a#doc": 1}, "q2": {"b#doc": 1}})
    assert qrels.subset(["q2"]).query_ids == ["q2"]


def test_run_to_ranx_encodes_rank_as_decreasing_score() -> None:
    import pytest

    pytest.importorskip("ranx")
    run = TrecRun({"q1": [("a#doc", 0.01), ("b#doc", 0.009)]}, "t")
    ranx_run = run.to_ranx()
    scores = ranx_run.to_dict()["q1"]
    # rank encoded so the top doc always outscores the next, whatever the raw
    # fused scores were (guards the determinism contract through ranx).
    assert scores["a#doc"] > scores["b#doc"]
