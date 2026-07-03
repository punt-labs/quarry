"""Tests for tools/oo_score.py — the OO ratchet that gates every commit."""

from __future__ import annotations

import importlib.util
import json
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType


def _raise_oserror(*_args: object, **_kwargs: object) -> None:
    raise OSError("disk full")


def _load_oo_score() -> ModuleType:
    """Import tools/oo_score.py as a module.

    The tool lives outside src/quarry/, so it has no normal import path.
    importlib.util keeps the test independent of tools/ layout changes.
    """
    import pathlib

    root = pathlib.Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "_oo_score", root / "tools" / "oo_score.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_oo_score"] = module
    spec.loader.exec_module(module)
    return module


oo = _load_oo_score()


# A minimal, standards-clean source that scores identically every run.
_SAMPLE_SOURCE = '''\
"""Sample module."""

from __future__ import annotations

from typing import Self


class Widget:
    """A widget."""

    _size: int

    def __new__(cls, size: int) -> Self:
        self = super().__new__(cls)
        self._size = size
        return self

    @property
    def size(self) -> int:
        return self._size
'''


def _write_baseline(root: Path, data: dict[str, dict[str, float]]) -> None:
    (root / oo.Ratchet.BASELINE_FILE).write_text(json.dumps(data, indent=2) + "\n")


def _true_metrics(scorer: object, key: str) -> dict[str, float]:
    results = scorer.results  # type: ignore[attr-defined]
    by_file: dict[str, dict[str, float]] = oo.Ratchet._results_by_file(results)
    return by_file[key]


class TestVerifyPhantomGuard:
    """--verify: the committed baseline must match the committed code."""

    def _sample_repo(self, tmp_path: Path) -> tuple[object, str]:
        src = tmp_path / "mod.py"
        src.write_text(_SAMPLE_SOURCE)
        scorer = oo.Scorer(tmp_path)
        return scorer, str(src)

    def test_matching_baseline_passes(self, tmp_path: Path) -> None:
        scorer, key = self._sample_repo(tmp_path)
        _write_baseline(tmp_path, {key: _true_metrics(scorer, key)})
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.verify(scorer) == 0

    def test_phantom_detected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A baseline entry that never matched its code fails at verify time."""
        scorer, key = self._sample_repo(tmp_path)
        metrics = _true_metrics(scorer, key)
        # Phantom: baseline claims values the code never produced (cf.
        # thread_config 2/1.2 vs the real 3/1.6 that sat dormant for weeks).
        metrics["classes_per_module"] = metrics["classes_per_module"] + 1.0
        metrics["avg_complexity"] = metrics["avg_complexity"] + 0.4
        _write_baseline(tmp_path, {key: metrics})
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.verify(scorer) == 1
        out = capsys.readouterr().out
        assert "phantom" in out
        assert "classes_per_module" in out

    def test_stale_baseline_entry_detected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A baseline entry with no file on disk is an integrity failure."""
        scorer, key = self._sample_repo(tmp_path)
        baseline = {
            key: _true_metrics(scorer, key),
            "src/gone.py": {"module_size": 10.0},
        }
        _write_baseline(tmp_path, baseline)
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.verify(scorer) == 1
        assert "stale" in capsys.readouterr().out

    def test_unrecorded_file_detected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A scored file missing from the baseline is an integrity failure."""
        scorer, key = self._sample_repo(tmp_path)
        _write_baseline(tmp_path, {"src/other.py": _true_metrics(scorer, key)})
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.verify(scorer) == 1
        out = capsys.readouterr().out
        assert "unrecorded" in out

    def test_missing_baseline_fails_closed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing baseline is maximal divergence — deleting it must NOT pass.

        Otherwise removing .oo-baseline.json greens both --check and --verify
        and silently disables the ratchet.
        """
        scorer, _ = self._sample_repo(tmp_path)
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.verify(scorer) == 1
        assert "no baseline" in capsys.readouterr().out.lower()

    def test_missing_baseline_allow_missing_bootstrap_passes(
        self, tmp_path: Path
    ) -> None:
        """--allow-missing is the explicit escape hatch for first-time bootstrap."""
        scorer, _ = self._sample_repo(tmp_path)
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.verify(scorer, allow_missing=True) == 0


class TestScopedCorrection:
    """--correct: fix ONE phantom entry, with a mandatory audited reason."""

    def _phantom_repo(self, tmp_path: Path) -> tuple[str, dict[str, float]]:
        src = tmp_path / "mod.py"
        src.write_text(_SAMPLE_SOURCE)
        scorer = oo.Scorer(tmp_path)
        key = str(src)
        true_metrics = _true_metrics(scorer, key)
        phantom = dict(true_metrics)
        phantom["classes_per_module"] = true_metrics["classes_per_module"] + 1.0
        # A second, untouched file must survive the scoped correction intact.
        _write_baseline(tmp_path, {key: phantom, "src/other.py": {"module_size": 42.0}})
        return key, true_metrics

    def test_correct_records_true_score(self, tmp_path: Path) -> None:
        key, true_metrics = self._phantom_repo(tmp_path)
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.correct(key, "verify flagged a phantom from PR #292") == 0
        written = json.loads((tmp_path / oo.Ratchet.BASELINE_FILE).read_text())
        assert written[key]["classes_per_module"] == true_metrics["classes_per_module"]
        # The scope is exactly one file — the sibling entry is untouched.
        assert written["src/other.py"] == {"module_size": 42.0}

    def test_correct_appends_audit_reason(self, tmp_path: Path) -> None:
        key, _ = self._phantom_repo(tmp_path)
        ratchet = oo.Ratchet(tmp_path)
        reason = "verify flagged a phantom from PR #292"
        ratchet.correct(key, reason)
        audit = tmp_path / oo.Ratchet.AUDIT_FILE
        entry = json.loads(audit.read_text().splitlines()[-1])
        assert entry["verdict"] == "correct"
        assert entry["reason"] == reason
        assert entry["files_scored"] == 1

    def test_correct_refuses_empty_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No reason => no correction. The guardrail against silent laundering."""
        key, _ = self._phantom_repo(tmp_path)
        before = (tmp_path / oo.Ratchet.BASELINE_FILE).read_text()
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.correct(key, "") == 2
        assert "requires a non-empty --reason" in capsys.readouterr().out
        # Baseline untouched and no audit entry written.
        assert (tmp_path / oo.Ratchet.BASELINE_FILE).read_text() == before
        assert not (tmp_path / oo.Ratchet.AUDIT_FILE).exists()

    def test_correct_refuses_whitespace_reason(self, tmp_path: Path) -> None:
        key, _ = self._phantom_repo(tmp_path)
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.correct(key, "   ") == 2

    def test_correct_refuses_flag_like_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--reason --check` (value omitted, next flag swallowed) fails loud."""
        key, _ = self._phantom_repo(tmp_path)
        before = (tmp_path / oo.Ratchet.BASELINE_FILE).read_text()
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.correct(key, "--check") == 2
        assert "looks like a flag" in capsys.readouterr().out
        assert (tmp_path / oo.Ratchet.BASELINE_FILE).read_text() == before
        assert not (tmp_path / oo.Ratchet.AUDIT_FILE).exists()

    def test_correct_audits_before_persisting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed baseline write must still leave the audit record behind.

        Audit-before-save guarantees a correction never persists without its
        accountability line.
        """
        key, _ = self._phantom_repo(tmp_path)
        before = (tmp_path / oo.Ratchet.BASELINE_FILE).read_text()
        ratchet = oo.Ratchet(tmp_path)
        monkeypatch.setattr(ratchet, "_save_baseline", _raise_oserror)
        with pytest.raises(OSError, match="disk full"):
            ratchet.correct(key, "phantom fix")
        audit = tmp_path / oo.Ratchet.AUDIT_FILE
        assert audit.exists()
        entry = json.loads(audit.read_text().splitlines()[-1])
        assert entry["verdict"] == "correct"
        # Save raised, so the baseline on disk is unchanged.
        assert (tmp_path / oo.Ratchet.BASELINE_FILE).read_text() == before

    def test_correct_missing_file_returns_2(self, tmp_path: Path) -> None:
        self._phantom_repo(tmp_path)
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.correct(str(tmp_path / "nope.py"), "reason") == 2

    def test_corrected_baseline_then_verifies(self, tmp_path: Path) -> None:
        """After a correction, --verify must pass — the phantom is gone."""
        key, _ = self._phantom_repo(tmp_path)
        oo.Ratchet(tmp_path).correct(key, "phantom fix")
        # Drop the synthetic sibling so verify sees only the real file.
        baseline = json.loads((tmp_path / oo.Ratchet.BASELINE_FILE).read_text())
        del baseline["src/other.py"]
        _write_baseline(tmp_path, baseline)
        scorer = oo.Scorer(tmp_path)
        assert oo.Ratchet(tmp_path).verify(scorer) == 0


def _fake_scorer(fpath: str, metrics: dict[str, float]) -> object:
    """A stand-in exposing .results — check()/update() read only that."""
    return SimpleNamespace(results=[{"file": fpath, **metrics}])


class TestRatioMetricTolerance:
    """Ratio metrics may micro-regress under a real structural improvement."""

    def _prepare(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        baseline: dict[str, float],
    ) -> None:
        _write_baseline(tmp_path, {"src/x.py": baseline})
        # Compare every scored file, not only git-touched ones.
        monkeypatch.setattr(
            oo.Ratchet, "_git_touched_files", staticmethod(lambda: None)
        )

    def test_zero_param_extraction_artifact_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """avg_params 0.10 -> 0.11 with module_size shrinking is not a regression."""
        self._prepare(tmp_path, monkeypatch, {"avg_params": 0.10, "module_size": 100.0})
        scorer = _fake_scorer("src/x.py", {"avg_params": 0.11, "module_size": 95.0})
        assert oo.Ratchet(tmp_path).check(scorer) == 0

    def test_real_ratio_blowout_past_threshold_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ratio jump beyond epsilon is a genuine regression even with a win."""
        self._prepare(tmp_path, monkeypatch, {"avg_params": 3.90, "module_size": 100.0})
        scorer = _fake_scorer("src/x.py", {"avg_params": 4.50, "module_size": 95.0})
        assert oo.Ratchet(tmp_path).check(scorer) == 1

    def test_no_companion_improvement_not_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ratio nudge alone — no size/complexity win — stays a regression."""
        self._prepare(tmp_path, monkeypatch, {"avg_params": 0.10, "module_size": 100.0})
        scorer = _fake_scorer("src/x.py", {"avg_params": 0.11, "module_size": 100.0})
        assert oo.Ratchet(tmp_path).check(scorer) == 1

    def test_teetering_at_threshold_not_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tiny nudge is refused when the metric only barely clears threshold."""
        self._prepare(tmp_path, monkeypatch, {"avg_params": 3.99, "module_size": 100.0})
        scorer = _fake_scorer("src/x.py", {"avg_params": 4.00, "module_size": 95.0})
        assert oo.Ratchet(tmp_path).check(scorer) == 1

    def test_non_ratio_metric_never_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """module_size is not a ratio — a size regression is never absorbed."""
        self._prepare(
            tmp_path, monkeypatch, {"module_size": 100.0, "max_complexity": 8.0}
        )
        # module_size worsens by 1 line while max_complexity improves; strict.
        scorer = _fake_scorer("src/x.py", {"module_size": 101.0, "max_complexity": 7.0})
        assert oo.Ratchet(tmp_path).check(scorer) == 1

    def test_update_tolerates_and_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--update accepts the artifact and writes the new ratio value."""
        self._prepare(tmp_path, monkeypatch, {"avg_params": 0.10, "module_size": 100.0})
        scorer = _fake_scorer("src/x.py", {"avg_params": 0.11, "module_size": 95.0})
        assert oo.Ratchet(tmp_path).update(scorer) == 0
        written = json.loads((tmp_path / oo.Ratchet.BASELINE_FILE).read_text())
        assert written["src/x.py"]["avg_params"] == 0.11

    def test_update_refuses_real_blowout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._prepare(tmp_path, monkeypatch, {"avg_params": 3.90, "module_size": 100.0})
        scorer = _fake_scorer("src/x.py", {"avg_params": 4.50, "module_size": 95.0})
        assert oo.Ratchet(tmp_path).update(scorer) == 1
