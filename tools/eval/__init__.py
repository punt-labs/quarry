"""Phase-1 retrieval evaluation harness (glue around the frozen retrieval seam).

Measures the shipped ``HybridRetriever`` — it never modifies it. Value objects:
``JudgedUnit`` (the join key), ``TrecRun``/``Qrels`` (TREC I/O), ``Corpus``,
``QuerySet``, ``EvalRunner``, ``Scorer``, and the ``Provenance`` stamp.
"""

from __future__ import annotations

# First, before any numpy/onnxruntime/quarry import: pin BLAS/OMP to one thread.
# Importing this submodule runs ThreadPins.pin() as a side effect, so the pins
# are set before the heavy imports below load numpy and size its thread pools.
from tools.eval import _threadpins as _threadpins
from tools.eval.corpus import Corpus
from tools.eval.indexing import Collapser, EphemeralIndex
from tools.eval.judged_unit import JudgedUnit
from tools.eval.metrics import Scorer
from tools.eval.pollution import MetadataPollutionClassifier
from tools.eval.provenance import Determinism, Provenance
from tools.eval.queryset import Query, QuerySet
from tools.eval.report import BucketReport, EvalReport, MetricScores
from tools.eval.runner import EvalRunner, RunOutput
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
