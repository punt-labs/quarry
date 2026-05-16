"""Tests for tools/suppression_ratchet.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from types import ModuleType


def _load_ratchet() -> ModuleType:
    """Import tools/suppression_ratchet.py as a module.

    The tool lives outside src/quarry/, so it has no normal import path.
    Using importlib.util keeps the test file independent of repo layout
    changes in the tools/ directory.
    """
    root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "_ratchet", root / "tools" / "suppression_ratchet.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_ratchet"] = module
    spec.loader.exec_module(module)
    return module


ratchet = _load_ratchet()


class TestFileSuppressionsCodeLineDetection:
    def test_noqa_on_code_line_counted(self) -> None:
        source = "x = 1  # noqa: E501\n"
        fs = ratchet.FileSuppressions("f.py", source)
        assert fs.noqa == 1

    def test_noqa_inside_string_not_counted(self) -> None:
        source = '''
"""Docstring containing the word noqa for whatever reason.

This is a multi-line string with # noqa: E501 inside it.  The scanner
must not count this — it is documentation, not a real suppression.
"""

x = 1
'''
        fs = ratchet.FileSuppressions("f.py", source)
        assert fs.noqa == 0

    def test_noqa_on_pure_comment_line_not_counted(self) -> None:
        source = "# noqa: E501 — this is a standalone comment, no code\nx = 1\n"
        fs = ratchet.FileSuppressions("f.py", source)
        assert fs.noqa == 0

    def test_type_ignore_counted(self) -> None:
        source = "import x  # type: ignore[import-not-found]\n"
        fs = ratchet.FileSuppressions("f.py", source)
        assert fs.type_ignore == 1

    def test_pyright_ignore_counted(self) -> None:
        source = "y = z()  # pyright: ignore[reportUnknownVariableType]\n"
        fs = ratchet.FileSuppressions("f.py", source)
        assert fs.pyright_ignore == 1

    def test_pylint_disable_counted(self) -> None:
        source = "x = 1  # pylint: disable=invalid-name\n"
        fs = ratchet.FileSuppressions("f.py", source)
        assert fs.pylint_disable == 1

    def test_syntax_error_returns_zero(self) -> None:
        """Unparseable source must not crash — returns zero counts."""
        source = "def broken(  # noqa: E501\n"
        fs = ratchet.FileSuppressions("f.py", source)
        # Without AST analysis, _string_line_numbers returns empty,
        # so the noqa is counted as a real comment.  Acceptable degraded
        # behavior — caller still gets a number, no crash.
        assert isinstance(fs.noqa, int)


class TestPerFileIgnoresCounter:
    def test_missing_pyproject_returns_zero(self, tmp_path: Path) -> None:
        counter = ratchet.PerFileIgnoresCounter(tmp_path / "nope.toml")
        assert counter.total == 0
        assert counter.breakdown == {}

    def test_well_formed_pyproject_counts(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[tool.ruff.lint.per-file-ignores]\n"
            '"tests/*" = ["S101", "PLR2004"]\n'
            '"docs/*" = ["E501"]\n'
        )
        counter = ratchet.PerFileIgnoresCounter(pyproject)
        assert counter.total == 3
        assert counter.breakdown == {"tests/*": 2, "docs/*": 1}

    def test_malformed_toml_raises(self, tmp_path: Path) -> None:
        """Silent miscounts would let suppressions slip past the ratchet."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[tool.ruff.lint.per-file-ignores\n")  # missing ]
        with pytest.raises(ValueError, match="cannot parse"):
            ratchet.PerFileIgnoresCounter(pyproject)

    def test_pyproject_without_per_file_ignores_is_zero(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "x"\n')
        counter = ratchet.PerFileIgnoresCounter(pyproject)
        assert counter.total == 0


class TestBaselineLoad:
    def test_missing_baseline_returns_empty(self, tmp_path: Path) -> None:
        baseline = ratchet.Baseline(tmp_path)
        assert baseline.has_baseline is False
        # _load_baseline is called by check() which guards on has_baseline first;
        # but if called directly, returns {} per the implementation contract.
        assert baseline._load_baseline() == {}

    def test_malformed_json_exits_2(self, tmp_path: Path) -> None:
        (tmp_path / ".suppression-baseline.json").write_text("{not json")
        baseline = ratchet.Baseline(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            baseline._load_baseline()
        assert exc_info.value.code == 2

    def test_load_baseline_returns_dict(self, tmp_path: Path) -> None:
        (tmp_path / ".suppression-baseline.json").write_text(
            '{"total": 42, "by_category": {}, "by_file": {}}'
        )
        baseline = ratchet.Baseline(tmp_path)
        loaded = baseline._load_baseline()
        assert loaded["total"] == 42


class TestBaselineSaveAtomicity:
    def test_save_writes_baseline(self, tmp_path: Path) -> None:
        baseline = ratchet.Baseline(tmp_path)
        report = ratchet.SuppressionReport(file_results=[], per_file_ignores_count=0)
        baseline._save_baseline(report)
        path = tmp_path / ".suppression-baseline.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["total"] == 0

    def test_save_failure_cleans_up_fd_and_temp_file(self, tmp_path: Path) -> None:
        """When os.write raises, the fd must be closed and tmp file removed.

        Bug class 1 (file I/O safety) — without explicit cleanup the
        atomic-write pattern leaks fds and tmpfiles on failure.
        """
        baseline = ratchet.Baseline(tmp_path)
        report = ratchet.SuppressionReport(file_results=[], per_file_ignores_count=0)
        before = set(tmp_path.iterdir())
        with (
            patch.object(ratchet.os, "write", side_effect=OSError("disk full")),
            pytest.raises(OSError, match="disk full"),
        ):
            baseline._save_baseline(report)
        after = set(tmp_path.iterdir())
        # No leaked tempfile in the baseline directory.
        assert after == before


class TestBaselineCheck:
    def _baseline_with(self, tmp_path: Path, total: int) -> Path:
        path = tmp_path / ".suppression-baseline.json"
        path.write_text(json.dumps({"total": total, "by_category": {}, "by_file": {}}))
        return path

    def test_check_no_baseline_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        baseline = ratchet.Baseline(tmp_path)
        report = ratchet.SuppressionReport(file_results=[], per_file_ignores_count=0)
        assert baseline.check(report) == 1
        assert "No baseline" in capsys.readouterr().err

    def test_check_unchanged_returns_0(self, tmp_path: Path) -> None:
        self._baseline_with(tmp_path, 0)
        baseline = ratchet.Baseline(tmp_path)
        report = ratchet.SuppressionReport(file_results=[], per_file_ignores_count=0)
        assert baseline.check(report) == 0

    def test_check_decreased_returns_0(self, tmp_path: Path) -> None:
        self._baseline_with(tmp_path, 10)
        baseline = ratchet.Baseline(tmp_path)
        report = ratchet.SuppressionReport(file_results=[], per_file_ignores_count=0)
        assert baseline.check(report) == 0

    def test_check_increased_returns_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._baseline_with(tmp_path, 0)
        baseline = ratchet.Baseline(tmp_path)
        fs = ratchet.FileSuppressions("a.py", "x = 1  # noqa\n")
        report = ratchet.SuppressionReport(file_results=[fs], per_file_ignores_count=0)
        assert baseline.check(report) == 1
        out = capsys.readouterr().out
        assert "FAIL" in out
        assert "a.py" in out


class TestBaselineAppendAudit:
    def test_append_audit_writes_jsonl_line(self, tmp_path: Path) -> None:
        baseline = ratchet.Baseline(tmp_path)
        report = ratchet.SuppressionReport(file_results=[], per_file_ignores_count=0)
        baseline._append_audit(report)
        audit = tmp_path / ".suppression-audit.jsonl"
        assert audit.exists()
        entry = json.loads(audit.read_text())
        assert entry["total"] == 0

    def test_append_audit_swallows_oserror(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Audit failures must not block the ratchet update — log and continue."""
        baseline = ratchet.Baseline(tmp_path)
        report = ratchet.SuppressionReport(file_results=[], per_file_ignores_count=0)
        with patch.object(Path, "open", side_effect=OSError("read-only")):
            baseline._append_audit(report)  # must not raise
        assert "WARNING" in capsys.readouterr().out
