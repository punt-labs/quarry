"""Phase-1 retrieval evaluation harness (glue around the frozen retrieval seam).

Measures the shipped ``HybridRetriever`` — it never modifies it. Value objects:
``JudgedUnit`` (the join key), ``TrecRun``/``Qrels`` (TREC I/O), ``Corpus``,
``QuerySet``, ``EvalRunner``, ``Scorer``, and the ``Provenance`` stamp.
"""

from __future__ import annotations

from tools.eval.corpus import Corpus
from tools.eval.judged_unit import JudgedUnit
from tools.eval.metrics import BucketReport, EvalReport, MetricScores, Scorer
from tools.eval.pollution import MetadataPollutionClassifier
from tools.eval.provenance import Determinism, Provenance
from tools.eval.queryset import Query, QuerySet
from tools.eval.runner import Collapser, EphemeralIndex, EvalRunner, RunOutput
from tools.eval.trec import Qrels, TrecRun

__all__ = [
    "BucketReport",
    "Collapser",
    "Corpus",
    "Determinism",
    "EphemeralIndex",
    "EvalReport",
    "EvalRunner",
    "JudgedUnit",
    "MetadataPollutionClassifier",
    "MetricScores",
    "Provenance",
    "Qrels",
    "Query",
    "QuerySet",
    "RunOutput",
    "Scorer",
    "TrecRun",
]
