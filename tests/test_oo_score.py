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

    def test_scan_error_reported_not_stale(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A file that no longer parses reports as a scan error, not as stale.

        _results_by_file drops the errored entry, so without explicit handling
        the baseline entry would masquerade as a 'stale' row and hide the real
        cause.
        """
        src = tmp_path / "mod.py"
        src.write_text(_SAMPLE_SOURCE)
        key = str(src)
        _write_baseline(tmp_path, {key: _true_metrics(oo.Scorer(tmp_path), key)})
        src.write_text("def broken(:\n")  # unparseable
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.verify(oo.Scorer(tmp_path)) == 1
        out = capsys.readouterr().out
        assert "scan error" in out
        assert "stale" not in out

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

    def test_correct_matches_by_resolved_path(self, tmp_path: Path) -> None:
        """A non-canonical FILE= (here a symlink) corrects the existing entry.

        Without resolved-path matching, `str(Path(alias))` keys a NEW entry
        that the next --verify flags as stale/phantom.
        """
        key, true_metrics = self._phantom_repo(tmp_path)
        alias = tmp_path / "alias.py"
        alias.symlink_to(tmp_path / "mod.py")
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.correct(str(alias), "phantom fix via alias") == 0
        written = json.loads((tmp_path / oo.Ratchet.BASELINE_FILE).read_text())
        # Corrected in place — no duplicate "alias.py" entry was added.
        assert set(written) == {key, "src/other.py"}
        assert written[key]["classes_per_module"] == true_metrics["classes_per_module"]

    def test_correct_unknown_file_refuses(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--correct on a file absent from the baseline fails, never silently adds."""
        self._phantom_repo(tmp_path)
        extra = tmp_path / "extra.py"
        extra.write_text(_SAMPLE_SOURCE)
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.correct(str(extra), "should fail") == 2
        assert "no baseline entry" in capsys.readouterr().out
        written = json.loads((tmp_path / oo.Ratchet.BASELINE_FILE).read_text())
        assert str(extra) not in written


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
        """module_size is not a ratio — an over-target grow is never absorbed.

        Uses over-300 values so the grow is a genuine regression under the
        asymmetric module_size rule (below 300 a grow is allowed, so 100 -> 101
        would no longer be a regression to test against).
        """
        self._prepare(
            tmp_path, monkeypatch, {"module_size": 350.0, "max_complexity": 8.0}
        )
        # module_size worsens by 1 line above target while max_complexity
        # improves; ratio tolerance must not absorb the size regression.
        scorer = _fake_scorer("src/x.py", {"module_size": 351.0, "max_complexity": 7.0})
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


class TestModuleSizeHeadroom:
    """module_size ratchet only bites ABOVE the 300 target (asymmetric).

    Below the target a file may grow (up to the target); over the target it
    must not grow vs baseline. A below-target grow is neutral — never an
    improvement — so it cannot satisfy the 'at least one metric improved' gate.
    """

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

    def test_below_target_grow_passes_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """133 -> 176 (both < 300) is not a regression; a companion win passes."""
        self._prepare(
            tmp_path, monkeypatch, {"module_size": 133.0, "max_complexity": 8.0}
        )
        scorer = _fake_scorer("src/x.py", {"module_size": 176.0, "max_complexity": 7.0})
        assert oo.Ratchet(tmp_path).check(scorer) == 0

    def test_below_target_grow_update_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--update accepts a below-target grow and writes the larger size."""
        self._prepare(tmp_path, monkeypatch, {"module_size": 133.0})
        scorer = _fake_scorer("src/x.py", {"module_size": 176.0})
        assert oo.Ratchet(tmp_path).update(scorer) == 0
        written = json.loads((tmp_path / oo.Ratchet.BASELINE_FILE).read_text())
        assert written["src/x.py"]["module_size"] == 176.0

    def test_over_target_grow_fails_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """310 -> 330 (both > 300) is a strict regression on a god-module."""
        self._prepare(tmp_path, monkeypatch, {"module_size": 310.0})
        scorer = _fake_scorer("src/x.py", {"module_size": 330.0})
        assert oo.Ratchet(tmp_path).check(scorer) == 1

    def test_over_target_grow_refused_by_update(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--update mirrors --check: an over-target grow is refused, not recorded."""
        self._prepare(tmp_path, monkeypatch, {"module_size": 310.0})
        scorer = _fake_scorer("src/x.py", {"module_size": 330.0})
        assert oo.Ratchet(tmp_path).update(scorer) == 1
        written = json.loads((tmp_path / oo.Ratchet.BASELINE_FILE).read_text())
        assert written["src/x.py"]["module_size"] == 310.0

    def test_over_target_shrink_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """350 -> 340 shrinks a god-module: a genuine improvement, check passes."""
        self._prepare(tmp_path, monkeypatch, {"module_size": 350.0})
        scorer = _fake_scorer("src/x.py", {"module_size": 340.0})
        assert oo.Ratchet(tmp_path).check(scorer) == 0

    def test_crossing_target_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """290 -> 310 crosses the target via growth — refused (current > 300)."""
        self._prepare(tmp_path, monkeypatch, {"module_size": 290.0})
        scorer = _fake_scorer("src/x.py", {"module_size": 310.0})
        assert oo.Ratchet(tmp_path).check(scorer) == 1

    def test_one_line_past_target_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """300 -> 301 fails: growth one line past the target is a regression."""
        self._prepare(tmp_path, monkeypatch, {"module_size": 300.0})
        scorer = _fake_scorer("src/x.py", {"module_size": 301.0})
        assert oo.Ratchet(tmp_path).check(scorer) == 1

    def test_grow_exactly_to_target_passes_size_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """299 -> 300 clears the size gate: growth to exactly the target is allowed.

        The grow is neutral (not an improvement), so alone — with nothing else
        improving — it still trips the 'nothing improved' gate. Both facts are
        asserted here to pin the exact boundary against future edits.
        """
        self._prepare(tmp_path, monkeypatch, {"module_size": 299.0})
        scorer = _fake_scorer("src/x.py", {"module_size": 300.0})
        # Size gate is not a regression, but nothing improved -> exit 1.
        assert oo.Ratchet(tmp_path).check(scorer) == 1
        # And --update records it — a below/at-target grow is not refused.
        assert oo.Ratchet(tmp_path).update(scorer) == 0
        written = json.loads((tmp_path / oo.Ratchet.BASELINE_FILE).read_text())
        assert written["src/x.py"]["module_size"] == 300.0

    def test_below_target_grow_alone_fails_nothing_improved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A below-target grow is NEUTRAL, not an improvement.

        With no other metric improving, the 'nothing improved' gate still
        fires — a bare size grow cannot green a commit on its own.
        """
        self._prepare(tmp_path, monkeypatch, {"module_size": 133.0})
        scorer = _fake_scorer("src/x.py", {"module_size": 176.0})
        assert oo.Ratchet(tmp_path).check(scorer) == 1

    def test_other_metric_gets_no_headroom(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only module_size has below-target headroom; max_complexity is strict.

        5 -> 6 (both < the 10 threshold) is still a regression — the asymmetry
        is module_size-specific.
        """
        self._prepare(
            tmp_path, monkeypatch, {"max_complexity": 5.0, "module_size": 100.0}
        )
        scorer = _fake_scorer("src/x.py", {"max_complexity": 6.0, "module_size": 100.0})
        assert oo.Ratchet(tmp_path).check(scorer) == 1
