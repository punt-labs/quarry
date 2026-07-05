"""Phase-1 report value objects: per-bucket scores, bucket reports, full report.

These carry the numbers ``Scorer`` computes and own their own serialization and
rendering — the JSON shape a committed baseline records and the text table the
CLI prints both live here as methods, not as free helpers around the classes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from tools.eval.provenance import Provenance


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

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-serializable row for one bucket (serialization boundary)."""
        return {
            "n_queries": self.n_queries,
            "n_scorable": self.n_scorable,
            "mrr": self.mrr,
            "success@5": self.success_at_5,
            "success@10": self.success_at_10,
            "judged@10": self.judged_at_10,
            "metadata_pollution@10": self.pollution_at_10,
        }

    def render_row(self, label: str) -> str:
        """Render this bucket's numbers as one aligned text row under *label*."""
        return (
            f"  {label:<12} {self.n_queries:>3} {self.n_scorable:>4} "
            f"{_fmt(self.mrr):>6} {_fmt(self.success_at_5):>7} "
            f"{_fmt(self.success_at_10):>8} "
            f"{_fmt(self.judged_at_10):>8} {self.pollution_at_10:>8.3f}"
        )

    @staticmethod
    def row_header() -> str:
        """Return the column header for the ``render_row`` layout."""
        return (
            f"  {'bucket':<12} {'n':>3} {'scor':>4} "
            f"{'MRR':>6} {'succ@5':>7} {'succ@10':>8} {'judg@10':>8} {'poll@10':>8}"
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
            "buckets": {b.bucket: b.scores.to_dict() for b in self.buckets},
            "overall": self.overall.to_dict(),
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
        rows = [b.scores.render_row(b.bucket) for b in self.buckets]
        rows.append(self.overall.render_row("OVERALL"))
        table = "\n".join(rows)
        return f"{header}\n{MetricScores.row_header()}\n{table}\n"


def _fmt(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:.3f}"
