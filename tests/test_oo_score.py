"""Tests for tools/oo_score.py — the OO ratchet that gates every commit."""

from __future__ import annotations

import importlib.util
import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    import pytest


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

    def test_no_baseline_is_trivial_pass(self, tmp_path: Path) -> None:
        scorer, _ = self._sample_repo(tmp_path)
        ratchet = oo.Ratchet(tmp_path)
        assert ratchet.verify(scorer) == 0


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
