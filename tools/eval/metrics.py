"""Phase-1 scoring: ranx MRR + success@k per bucket, plus pollution diagnostic.

nDCG is deliberately absent: under a single binary-relevant known-item it
degenerates to MRR, so a Phase-1 nDCG would be a misleading second axis (design
section 2). metadata-pollution@10 is a reported-only diagnostic, never gated.
Metrics are computed and reported per bucket (natural / known-item / regression)
and never blended into a single headline number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from quarry.results import SearchResult
    from tools.eval.pollution import MetadataPollutionClassifier
    from tools.eval.provenance import Provenance
    from tools.eval.queryset import QuerySet
    from tools.eval.trec import Qrels, TrecRun

_PHASE = "phase-1"
_SUCCESS_K = (5, 10)
_JUDGED_K = 10
_POLLUTION_K = 10


@dataclass(frozen=True, slots=True)
class MetricScores:
    """One bucket's (or the overall) Phase-1 numbers.

    The rank metrics are ``None`` exactly when the bucket carries no known-item
    answer (natural queries pre-Phase-2): absence of a qrel is a documented
    state, not a failure to compute. Pollution is always defined — it needs no
    qrel, only the retrieved chunks.
    """

    n_queries: int
    n_scorable: int
    # None == "no qrels for this bucket yet" (natural bucket, Phase 1). PY-TS-14:
    # the absence is the documented Phase-1 contract, not a giving-up.
    mrr: float | None
    success_at_5: float | None
    success_at_10: float | None
    judged_at_10: float | None
    pollution_at_10: float

    @classmethod
    def unscorable(cls, n_queries: int, pollution_at_10: float) -> Self:
        """Build scores for a bucket with no known-item answers."""
        return cls(
            n_queries=n_queries,
            n_scorable=0,
            mrr=None,
            success_at_5=None,
            success_at_10=None,
            judged_at_10=None,
            pollution_at_10=pollution_at_10,
        )


@dataclass(frozen=True, slots=True)
class BucketReport:
    """A bucket label paired with its scores."""

    bucket: str
    scores: MetricScores


@dataclass(frozen=True, slots=True)
class EvalReport:
    """A full Phase-1 report for one config: per-bucket scores plus provenance."""

    config_tag: str
    phase: str
    provenance: Provenance
    buckets: tuple[BucketReport, ...]
    overall: MetricScores

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-serializable baseline record (serialization boundary)."""
        return {
            "config_tag": self.config_tag,
            "phase": self.phase,
            "provenance": self.provenance.to_dict(),
            "metrics_note": (
                "MRR/success@k only; nDCG omitted (degenerate under "
                "single-relevant known-item). metadata-pollution@10 is a "
                "reported-only diagnostic."
            ),
            "buckets": {b.bucket: _scores_dict(b.scores) for b in self.buckets},
            "overall": _scores_dict(self.overall),
        }

    def render(self) -> str:
        """Render a phase-labeled, per-bucket text report."""
        header = (
            f"Phase-1 retrieval metrics  [config={self.config_tag}]\n"
            "  primary: MRR, success@5, success@10 "
            "(nDCG omitted: degenerate on single-relevant known-item)\n"
            "  diagnostic: metadata-pollution@10 (reported, never gated)\n"
            f"  provenance: {self.provenance.to_dict()}\n"
        )
        rows = [_render_row(b.bucket, b.scores) for b in self.buckets]
        rows.append(_render_row("OVERALL", self.overall))
        table = "\n".join(rows)
        return f"{header}\n{_row_header()}\n{table}\n"


def _scores_dict(scores: MetricScores) -> dict[str, object]:
    return {
        "n_queries": scores.n_queries,
        "n_scorable": scores.n_scorable,
        "mrr": scores.mrr,
        "success@5": scores.success_at_5,
        "success@10": scores.success_at_10,
        "judged@10": scores.judged_at_10,
        "metadata_pollution@10": scores.pollution_at_10,
    }


def _fmt(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:.3f}"


def _row_header() -> str:
    return (
        f"  {'bucket':<12} {'n':>3} {'scor':>4} "
        f"{'MRR':>6} {'succ@5':>7} {'succ@10':>8} {'judg@10':>8} {'poll@10':>8}"
    )


def _render_row(label: str, s: MetricScores) -> str:
    return (
        f"  {label:<12} {s.n_queries:>3} {s.n_scorable:>4} "
        f"{_fmt(s.mrr):>6} {_fmt(s.success_at_5):>7} {_fmt(s.success_at_10):>8} "
        f"{_fmt(s.judged_at_10):>8} {s.pollution_at_10:>8.3f}"
    )


class Scorer:
    """Compute per-bucket Phase-1 scores from a run, qrels, and retrieved chunks."""

    __slots__ = ("_classifier", "_queryset")

    _queryset: QuerySet
    _classifier: MetadataPollutionClassifier

    def __new__(
        cls, queryset: QuerySet, classifier: MetadataPollutionClassifier
    ) -> Self:
        self = super().__new__(cls)
        self._queryset = queryset
        self._classifier = classifier
        return self

    def score(
        self,
        run: TrecRun,
        qrels: Qrels,
        chunk_results: Mapping[str, Sequence[SearchResult]],
        tag: str,
        provenance: Provenance,
    ) -> EvalReport:
        """Score every bucket and the overall set into an EvalReport."""
        buckets = tuple(
            BucketReport(bucket, self._bucket_scores(bucket, run, qrels, chunk_results))
            for bucket in self._queryset.buckets()
        )
        overall = self._scores_for(
            [q.query_id for q in self._queryset],
            [q.query_id for q in self._queryset.scorable()],
            run,
            qrels,
            chunk_results,
        )
        return EvalReport(
            config_tag=tag,
            phase=_PHASE,
            provenance=provenance,
            buckets=buckets,
            overall=overall,
        )

    def _bucket_scores(
        self,
        bucket: str,
        run: TrecRun,
        qrels: Qrels,
        chunk_results: Mapping[str, Sequence[SearchResult]],
    ) -> MetricScores:
        queries = self._queryset.in_bucket(bucket)
        all_ids = [q.query_id for q in queries]
        scorable_ids = [q.query_id for q in queries if q.answer is not None]
        return self._scores_for(all_ids, scorable_ids, run, qrels, chunk_results)

    def _scores_for(
        self,
        all_ids: Sequence[str],
        scorable_ids: Sequence[str],
        run: TrecRun,
        qrels: Qrels,
        chunk_results: Mapping[str, Sequence[SearchResult]],
    ) -> MetricScores:
        pollution = self._pollution(all_ids, chunk_results)
        if not scorable_ids:
            return MetricScores.unscorable(len(all_ids), pollution)
        ranked = self._ranx_scores(run, qrels, scorable_ids)
        return MetricScores(
            n_queries=len(all_ids),
            n_scorable=len(scorable_ids),
            mrr=ranked["mrr"],
            success_at_5=ranked["hit_rate@5"],
            success_at_10=ranked["hit_rate@10"],
            judged_at_10=self._judged_at_10(run, qrels, scorable_ids),
            pollution_at_10=pollution,
        )

    def _pollution(
        self, ids: Sequence[str], chunk_results: Mapping[str, Sequence[SearchResult]]
    ) -> float:
        present = [qid for qid in ids if qid in chunk_results]
        if not present:
            return 0.0
        ratios = [
            self._classifier.pollution_ratio(chunk_results[qid], _POLLUTION_K)
            for qid in present
        ]
        return sum(ratios) / len(ratios)

    @staticmethod
    def _judged_at_10(run: TrecRun, qrels: Qrels, ids: Sequence[str]) -> float:
        ratios: list[float] = []
        for qid in ids:
            if qid not in run.query_ids:
                ratios.append(0.0)
                continue
            top = [docid for docid, _score in run.ranking(qid)[:_JUDGED_K]]
            if not top:
                ratios.append(0.0)
                continue
            judged = qrels.judged_docids(qid)
            ratios.append(sum(1 for d in top if d in judged) / len(top))
        return sum(ratios) / len(ratios) if ratios else 0.0

    _RANX_METRICS = ("mrr", "hit_rate@5", "hit_rate@10")

    @classmethod
    def _ranx_scores(
        cls, run: TrecRun, qrels: Qrels, ids: Sequence[str]
    ) -> dict[str, float]:
        sub_run = run.subset(ids)
        # Every scorable query retrieved nothing: ranx cannot score an empty
        # run, and the honest result is a clean miss on every metric.
        if not any(sub_run.ranking(qid) for qid in sub_run.query_ids):
            return dict.fromkeys(cls._RANX_METRICS, 0.0)

        from ranx import evaluate  # noqa: PLC0415

        result = evaluate(
            qrels.subset(ids).to_ranx(), sub_run.to_ranx(), list(cls._RANX_METRICS)
        )
        # ranx returns a {metric: mean} mapping for a multi-metric request.
        return {name: float(result[name]) for name in cls._RANX_METRICS}
