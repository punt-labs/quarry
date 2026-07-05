"""Phase-1 scoring: ranx MRR + success@k per bucket, plus pollution diagnostic.

nDCG is deliberately absent: under a single binary-relevant known-item it
degenerates to MRR, so a Phase-1 nDCG would be a misleading second axis (design
section 2). metadata-pollution@10 is a reported-only diagnostic, never gated.
Metrics are computed and reported per bucket (natural / known-item / regression)
and never blended into a single headline number.

The report value objects (``MetricScores``/``BucketReport``/``EvalReport``) live
in ``report.py``; this module owns only the ``Scorer`` that produces them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from tools.eval.report import BucketReport, EvalReport, MetricScores

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
        # run at all, and the honest result is a clean miss on every metric.
        if not any(sub_run.ranking(qid) for qid in sub_run.query_ids):
            return dict.fromkeys(cls._RANX_METRICS, 0.0)

        from ranx import evaluate  # noqa: PLC0415

        # make_comparable=True is load-bearing for the MIXED case: a scorable
        # query that is in the qrels but retrieved nothing (so it is absent from
        # the run after to_ranx drops empty rankings) scores as a miss instead
        # of raising ranx's check_keys AssertionError. The all-empty guard above
        # only covers the case where EVERY scorable query is empty.
        result = evaluate(
            qrels.subset(ids).to_ranx(),
            sub_run.to_ranx(),
            list(cls._RANX_METRICS),
            make_comparable=True,
        )
        # ranx returns a {metric: mean} mapping for a multi-metric request.
        return {name: float(result[name]) for name in cls._RANX_METRICS}
