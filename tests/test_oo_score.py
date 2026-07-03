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
