"""Golden self-test: the harness over a 2-doc micro-fixture, fixed MRR/success.

Slow — it embeds with the real ONNX model, so it is excluded from the default
suite (``-m 'not slow'``) and CI. It is the harness carrying its own end-to-end
test: two unambiguous known-item queries must each retrieve their answer doc at
rank 1, giving MRR = success@5 = success@10 = 1.0 and zero pollution on clean
prose. A regression here means the measurement loop itself broke.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quarry.retrieval import RetrievalConfig

pytest.importorskip("ranx")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MICRO_FIXTURES = _REPO_ROOT / "tools" / "eval" / "fixtures_micro"
_MICRO_QUERIES = _REPO_ROOT / "tools" / "eval" / "queries_micro.jsonl"


@pytest.mark.slow
def test_golden_micro_fixture_scores_are_fixed(tmp_path: Path) -> None:
    from tools.eval.__main__ import Harness

    harness = Harness(_MICRO_FIXTURES, _MICRO_QUERIES, tmp_path / "index")
    report, _output = harness.evaluate(RetrievalConfig(exact_search=True))

    known = next(b for b in report.buckets if b.bucket == "known-item").scores
    assert known.n_scorable == 2
    assert known.mrr == pytest.approx(1.0)
    assert known.success_at_5 == pytest.approx(1.0)
    assert known.success_at_10 == pytest.approx(1.0)
    assert known.judged_at_10 == pytest.approx(0.5)  # 1 judged of 2 retrieved docs
    assert known.pollution_at_10 == pytest.approx(0.0)  # clean prose, no metadata


@pytest.mark.slow
def test_golden_run_is_deterministic(tmp_path: Path) -> None:
    from tools.eval.__main__ import Harness

    config = RetrievalConfig(exact_search=True)
    run_a = Harness(_MICRO_FIXTURES, _MICRO_QUERIES, tmp_path / "a")
    run_b = Harness(_MICRO_FIXTURES, _MICRO_QUERIES, tmp_path / "b")
    first, _ = run_a.evaluate(config)
    second, _ = run_b.evaluate(config)
    assert first.to_dict()["buckets"] == second.to_dict()["buckets"]
