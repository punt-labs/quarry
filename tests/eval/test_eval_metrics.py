"""Unit tests for the Scorer: per-bucket MRR/success@k + pollution, via ranx."""

from __future__ import annotations

import pytest

from quarry.results import SearchResult
from tools.eval.judged_unit import JudgedUnit
from tools.eval.metrics import Scorer
from tools.eval.pollution import MetadataPollutionClassifier
from tools.eval.provenance import Provenance
from tools.eval.queryset import Query, QuerySet
from tools.eval.report import BucketReport
from tools.eval.trec import Qrels, TrecRun

pytest.importorskip("ranx")

_PROSE = "Binary search halves a sorted range each step, so it runs in log time."


def _prose_chunk() -> SearchResult:
    return SearchResult(
        document_name="x.md",
        collection="eval",
        page_number=1,
        chunk_index=0,
        text=_PROSE,
        page_type="section",
        source_format=".md",
        agent_handle="",
        memory_type="",
        summary="",
        distance=0.2,
    )


def _provenance() -> Provenance:
    return Provenance(
        ort_version="1.24.1",
        model_repo="repo",
        model_revision="rev",
        cpu_arch="arm64",
        numpy_version="2.4.6",
        intra_op_threads=1,
    )


def _bucket(report_buckets: tuple[BucketReport, ...], name: str) -> BucketReport:
    return next(b for b in report_buckets if b.bucket == name)


def _queryset() -> QuerySet:
    return QuerySet(
        (
            Query("q1", "a", "known-item", "test", JudgedUnit("a.md", None)),
            Query("q2", "b", "known-item", "test", JudgedUnit("b.md", None)),
            Query("q3", "c", "natural", "test", None),
        )
    )


def test_known_item_bucket_mrr_and_success() -> None:
    queryset = _queryset()
    run = TrecRun(
        {
            "q1": [("a.md#doc", 1.0)],
            "q2": [("x.md#doc", 1.0), ("b.md#doc", 0.5)],
            "q3": [("z.md#doc", 1.0)],
        },
        "t",
    )
    chunks = {qid: [_prose_chunk(), _prose_chunk()] for qid in ("q1", "q2", "q3")}
    report = Scorer(queryset, MetadataPollutionClassifier()).score(
        run, queryset.to_qrels(), chunks, "t", _provenance()
    )

    known = _bucket(report.buckets, "known-item").scores
    assert known.n_queries == 2
    assert known.n_scorable == 2
    assert known.mrr == pytest.approx(0.75)  # (1 + 1/2) / 2
    assert known.success_at_5 == pytest.approx(1.0)
    assert known.success_at_10 == pytest.approx(1.0)
    assert known.judged_at_10 == pytest.approx(0.75)  # (1/1 + 1/2) / 2
    assert known.pollution_at_10 == pytest.approx(0.0)


def test_natural_bucket_is_unscored_but_reports_pollution() -> None:
    queryset = _queryset()
    run = TrecRun({"q3": [("z.md#doc", 1.0)]}, "t")
    chunks = {"q3": [_prose_chunk()]}
    report = Scorer(queryset, MetadataPollutionClassifier()).score(
        run, queryset.to_qrels(), chunks, "t", _provenance()
    )
    natural = _bucket(report.buckets, "natural").scores
    assert natural.mrr is None
    assert natural.n_scorable == 0
    assert natural.pollution_at_10 == pytest.approx(0.0)


def test_overall_aggregates_scorable_queries() -> None:
    queryset = _queryset()
    run = TrecRun(
        {"q1": [("a.md#doc", 1.0)], "q2": [("x.md#doc", 1.0), ("b.md#doc", 0.5)]},
        "t",
    )
    report = Scorer(queryset, MetadataPollutionClassifier()).score(
        run, queryset.to_qrels(), {}, "t", _provenance()
    )
    assert report.overall.mrr == pytest.approx(0.75)
    assert report.overall.n_scorable == 2


def test_report_to_dict_is_phase_labeled_and_serializable() -> None:
    queryset = _queryset()
    run = TrecRun({"q1": [("a.md#doc", 1.0)]}, "t")
    report = Scorer(queryset, MetadataPollutionClassifier()).score(
        run, Qrels({"q1": {"a.md#doc": 1}}), {}, "t", _provenance()
    )
    payload = report.to_dict()
    assert payload["phase"] == "phase-1"
    assert "nDCG omitted" in str(payload["metrics_note"])
    assert "known-item" in payload["buckets"]  # type: ignore[operator]


def test_report_render_has_a_header_and_one_row_per_bucket() -> None:
    queryset = _queryset()
    run = TrecRun({"q1": [("a.md#doc", 1.0)]}, "t")
    report = Scorer(queryset, MetadataPollutionClassifier()).score(
        run, queryset.to_qrels(), {}, "t", _provenance()
    )
    text = report.render()
    assert "Phase-1 retrieval metrics" in text
    assert "bucket" in text  # the row header
    for label in ("known-item", "natural", "OVERALL"):
        assert label in text


def test_scorable_query_with_empty_ranking_scores_as_miss() -> None:
    # MIXED empty case: q1 retrieves its answer, q2 (also scorable) retrieves
    # nothing. Without make_comparable ranx raises on the qrel/run mismatch;
    # with it, q2 is an honest miss (RR=0), not a crash.
    queryset = QuerySet(
        (
            Query("q1", "a", "known-item", "test", JudgedUnit("a.md", None)),
            Query("q2", "b", "known-item", "test", JudgedUnit("b.md", None)),
        )
    )
    run = TrecRun({"q1": [("a.md#doc", 1.0)], "q2": []}, "t")
    report = Scorer(queryset, MetadataPollutionClassifier()).score(
        run, queryset.to_qrels(), {}, "t", _provenance()
    )
    known = _bucket(report.buckets, "known-item").scores
    assert known.n_scorable == 2
    assert known.mrr == pytest.approx(0.5)  # (1.0 + 0) / 2
    assert known.success_at_5 == pytest.approx(0.5)  # 1 of 2 hit
    assert known.success_at_10 == pytest.approx(0.5)


def test_page_level_answer_joins_paged_run_row() -> None:
    # A page-level known-item: the qrel docid is the "#pN" form, and the run row
    # keyed on the same page joins end-to-end through the Scorer (doc-level joins
    # are covered elsewhere; this pins the page-granularity join).
    queryset = QuerySet(
        (Query("q1", "a", "known-item", "test", JudgedUnit("a.md", 2)),)
    )
    assert queryset.to_qrels().relevant_docids("q1") == {"a.md#p2"}
    run = TrecRun({"q1": [("a.md#p2", 1.0), ("a.md#p1", 0.5)]}, "t")
    report = Scorer(queryset, MetadataPollutionClassifier()).score(
        run, queryset.to_qrels(), {}, "t", _provenance()
    )
    known = _bucket(report.buckets, "known-item").scores
    assert known.n_scorable == 1
    assert known.mrr == pytest.approx(1.0)  # a.md#p2 at rank 1
    assert known.success_at_5 == pytest.approx(1.0)
