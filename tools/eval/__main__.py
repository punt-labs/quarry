"""``python -m tools.eval`` — run the Phase-1 harness and print per-bucket metrics.

Wires the fixture corpus, the query set, the frozen retrieval seam, and the
scorer into one report. ``--emit-baseline`` additionally writes the committed
baseline JSON plus the TREC run and qrels next to it, stamped with provenance.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Self

from quarry.config import Settings
from quarry.retrieval import RetrievalConfig
from tools.eval.corpus import Corpus
from tools.eval.metrics import Scorer
from tools.eval.pollution import MetadataPollutionClassifier
from tools.eval.provenance import Determinism, Provenance
from tools.eval.queryset import QuerySet
from tools.eval.runner import EvalRunner

if TYPE_CHECKING:
    from tools.eval.report import EvalReport
    from tools.eval.runner import RunOutput

_PKG_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PKG_DIR.parents[1]


class Harness:
    """Facade over the harness: build the corpus/queries, run, and score."""

    __slots__ = ("_corpus", "_queryset", "_scorer", "_workdir")

    _corpus: Corpus
    _queryset: QuerySet
    _scorer: Scorer
    _workdir: Path

    def __new__(cls, fixtures: Path, queries: Path, workdir: Path) -> Self:
        self = super().__new__(cls)
        self._corpus = Corpus(fixtures)
        self._queryset = QuerySet.from_path(queries)
        self._scorer = Scorer(self._queryset, MetadataPollutionClassifier())
        self._workdir = workdir
        return self

    @staticmethod
    def tag_for(config: RetrievalConfig) -> str:
        """Return a short run tag encoding the config's distinguishing knobs."""
        search = "exact" if config.exact_search else "ann"
        return (
            f"{config.embedding_strategy}-rrf{config.rrf_k}"
            f"-fm{config.fetch_multiplier}-{search}"
        )

    def evaluate(self, config: RetrievalConfig) -> tuple[EvalReport, RunOutput]:
        """Run the query set under *config* and score it into an EvalReport."""
        Determinism.apply()
        settings = Settings()
        tag = self.tag_for(config)
        runner = EvalRunner(self._corpus, self._queryset, settings, self._workdir)
        output = runner.run(config, tag)
        report = self._scorer.score(
            output.run, output.qrels, output.chunk_results, tag, Provenance.capture()
        )
        return report, output

    def emit_baseline(self, report: EvalReport, output: RunOutput, path: Path) -> None:
        """Write the baseline JSON plus the TREC run and qrels beside it."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        output.run.write(path.with_name(f"{report.config_tag}.run.trec"))
        output.qrels.write(path.with_name(f"{report.config_tag}.qrels.trec"))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tools.eval", description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=_PKG_DIR / "fixtures")
    parser.add_argument("--queries", type=Path, default=_PKG_DIR / "queries.jsonl")
    parser.add_argument("--workdir", type=Path, default=_REPO_ROOT / ".tmp" / "eval")
    parser.add_argument(
        "--ann",
        action="store_true",
        help="use ANN vector search (default: exact/flat, the determinism contract)",
    )
    parser.add_argument("--emit-baseline", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the harness and print the report; optionally emit the baseline."""
    logging.basicConfig(level=logging.WARNING)
    # The eval process forces OMP_NUM_THREADS=1 (below ThreadConfig's cap of 2),
    # so apply_env_limits logs a spurious DES-032 oversubscription warning. The
    # single thread is intentional here, not a defeated mitigation — silence it.
    logging.getLogger("quarry.thread_config").setLevel(logging.ERROR)
    args = _parse_args(argv)
    harness = Harness(args.fixtures, args.queries, args.workdir)
    config = RetrievalConfig(exact_search=not args.ann)
    report, output = harness.evaluate(config)
    print(report.render())
    if args.emit_baseline is not None:
        harness.emit_baseline(report, output, args.emit_baseline)
        print(f"baseline written: {args.emit_baseline}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
