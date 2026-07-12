"""Tests for the ported vox OO ratchet (``tools/oo_ratchet``).

The suite pins three things: the pure comparison/metric logic (``Thresholds``,
``ModuleMetrics``), the merge-base ``check`` semantics driven end-to-end through
a real git tree (the equivalence proof — known-good passes, known-bad classes of
regression fail), and the fail-closed boundaries (a corrupt base baseline blob
raises ``GitError`` rather than fail-open).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import tools.oo_ratchet
from tools.oo_ratchet import (
    GitError,
    GitRepo,
    ModuleMetrics,
    Ratchet,
    Scorer,
    Thresholds,
)

if TYPE_CHECKING:
    from tests.conftest import GitSandbox


def test_module_is_importable_as_package() -> None:
    """The package resolves as ``tools.oo_ratchet`` (regular-package path)."""
    assert "Ratchet" in tools.oo_ratchet.__all__


# A minimal, standards-clean module with hand-derived metrics.
_WIDGET_SOURCE = '''\
"""Doc."""

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


class TestThresholds:
    """The direction-aware comparison semantics per operator kind."""

    def test_meets_ge(self) -> None:
        assert Thresholds.meets("method_ratio", 0.80) is True
        assert Thresholds.meets("method_ratio", 0.79) is False

    def test_meets_le(self) -> None:
        assert Thresholds.meets("max_complexity", 10) is True
        assert Thresholds.meets("max_complexity", 11) is False

    def test_meets_eq(self) -> None:
        assert Thresholds.meets("init_violations", 0) is True
        assert Thresholds.meets("init_violations", 1) is False

    def test_better_or_equal_ge(self) -> None:
        assert Thresholds.better_or_equal("method_ratio", 0.9, 0.8) is True
        assert Thresholds.better_or_equal("method_ratio", 0.7, 0.8) is False

    def test_better_or_equal_le(self) -> None:
        # module_size is strict under vox — any growth regresses (no headroom).
        assert Thresholds.better_or_equal("module_size", 100, 100) is True
        assert Thresholds.better_or_equal("module_size", 101, 100) is False

    def test_better_or_equal_eq_is_distance_to_target(self) -> None:
        # future_annotations target is 1; 1 is as good as 1, 0 is worse.
        assert Thresholds.better_or_equal("future_annotations", 1, 1) is True
        assert Thresholds.better_or_equal("future_annotations", 0, 1) is False

    def test_strictly_better(self) -> None:
        assert Thresholds.strictly_better("max_complexity", 5, 6) is True
        assert Thresholds.strictly_better("max_complexity", 6, 6) is False


class TestModuleMetrics:
    """Golden per-module metric values on a known source."""

    def _metrics(self) -> dict[str, float | int | str]:
        return ModuleMetrics("widget.py", _WIDGET_SOURCE).compute()

    def test_structural_metrics(self) -> None:
        m = self._metrics()
        assert m["classes_per_module"] == 1
        assert m["method_ratio"] == 1.0  # 2 methods, 0 top-level functions
        assert m["class_to_func_ratio"] == 1.0
        assert m["future_annotations"] == 1
        assert m["init_violations"] == 0  # __new__, not __init__

    def test_encapsulation_metrics(self) -> None:
        m = self._metrics()
        assert m["encapsulation_ratio"] == 1.0  # self._size is private
        assert m["public_attr_violations"] == 0

    def test_param_and_complexity_metrics(self) -> None:
        m = self._metrics()
        # __new__(cls, size) -> 1 real param; size(self) -> 0. avg = 0.5.
        assert m["avg_params"] == 0.5
        assert m["max_complexity"] == 1


# --- merge-base check: real git tree, scored against the base-commit blob ---

_BASE_SOURCE = '''\
"""Calc."""

from __future__ import annotations

from typing import Self


class Calc:
    """A calculator."""

    _value: int

    def __new__(cls, value: int) -> Self:
        self = super().__new__(cls)
        self._value = value
        return self

    def classify(self, n: int) -> str:
        if n > 0:
            if n > 10:
                return "big"
            return "small"
        return "neg"
'''

# Extract-method style win: classify loses its branches, max_complexity drops.
_GOOD_SOURCE = '''\
"""Calc."""

from __future__ import annotations

from typing import Self


class Calc:
    """A calculator."""

    _value: int

    def __new__(cls, value: int) -> Self:
        self = super().__new__(cls)
        self._value = value
        return self

    def classify(self, n: int) -> str:
        return "unknown"
'''

# max_complexity regresses (3 -> 5) via added branches.
_BAD_COMPLEXITY_SOURCE = '''\
"""Calc."""

from __future__ import annotations

from typing import Self


class Calc:
    """A calculator."""

    _value: int

    def __new__(cls, value: int) -> Self:
        self = super().__new__(cls)
        self._value = value
        return self

    def classify(self, n: int) -> str:
        if n > 0:
            if n > 10:
                if n > 20:
                    if n > 30:
                        return "huge"
                    return "big"
                return "med"
            return "small"
        return "neg"
'''

# public_attr_violations 0 -> 1, encapsulation_ratio 1.0 -> 0.0.
_BAD_PUBLIC_ATTR_SOURCE = _BASE_SOURCE.replace("self._value", "self.value")

# module_size grows (strict under vox — any growth regresses).
_BAD_MODULE_SIZE_SOURCE = _BASE_SOURCE + "\n\nA = 1\nB = 2\nC = 3\nD = 4\nE = 5\n"


def _seat(argv: list[str]) -> int:
    """Score with a writer verb, tolerating the CI write guard."""
    return tools.oo_ratchet.main([*argv, "--allow-ci-write"])


def _check(base: str) -> int:
    """Run --check against a resolved base ref, fail-closed."""
    argv = ["src", "--check", "--base-ref", base, "--require-base"]
    return tools.oo_ratchet.main(argv)


def _check_against_base(
    sandbox: GitSandbox,
    monkeypatch: pytest.MonkeyPatch,
    base_src: str,
    head_src: str,
) -> int:
    """Seat base=base_src, move to head_src, and run --check against the base."""
    monkeypatch.chdir(sandbox.root)
    sandbox.write("src/mod.py", base_src)
    assert _seat(["src", "--rebaseline"]) == 0
    base = sandbox.commit("base")
    sandbox.write("src/mod.py", head_src)
    # Reseat the in-tree lock to the head scores; the comparison floor stays the
    # base-commit blob, so a laundered in-tree edit cannot lower it.
    assert _seat(["src", "--rebaseline"]) == 0
    return _check(base)


class TestMergeBaseCheck:
    """--check compares HEAD metrics against the base-commit baseline blob."""

    def test_known_good_diff_passes(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code = _check_against_base(git_sandbox, monkeypatch, _BASE_SOURCE, _GOOD_SOURCE)
        out = capsys.readouterr().out
        assert code == 0
        # Proves the base blob (not the reseated in-tree lock) was the floor:
        # against the in-tree lock the diff is a no-op and nothing would improve.
        assert "at least one metric improved" in out

    def test_bad_complexity_regression_fails(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code = _check_against_base(
            git_sandbox, monkeypatch, _BASE_SOURCE, _BAD_COMPLEXITY_SOURCE
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "regression detected" in out
        assert "max_complexity" in out

    def test_bad_public_attr_regression_fails(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code = _check_against_base(
            git_sandbox, monkeypatch, _BASE_SOURCE, _BAD_PUBLIC_ATTR_SOURCE
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "regression detected" in out
        assert "public_attr_violations" in out or "encapsulation_ratio" in out

    def test_bad_module_size_regression_fails(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code = _check_against_base(
            git_sandbox, monkeypatch, _BASE_SOURCE, _BAD_MODULE_SIZE_SOURCE
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "regression detected" in out
        assert "module_size" in out

    def test_require_base_unresolvable_fails_closed(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        git_sandbox.write("src/mod.py", _BASE_SOURCE)
        assert _seat(["src", "--rebaseline"]) == 0
        git_sandbox.commit("base")
        code = tools.oo_ratchet.main(
            ["src", "--check", "--base-ref", "nonexistent", "--require-base"]
        )
        assert code == 1
        assert "unresolvable" in capsys.readouterr().out


class TestAuditCompleteness:
    """--audit-completeness: every scored file must be in the in-tree baseline."""

    def test_missing_entry_fails(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        git_sandbox.write("src/mod.py", _WIDGET_SOURCE)
        git_sandbox.write(".oo-baseline.json", "{}\n")
        code = tools.oo_ratchet.main(["src", "--audit-completeness"])
        assert code == 1
        assert "missing from baseline" in capsys.readouterr().out

    def test_complete_baseline_passes(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        git_sandbox.write("src/mod.py", _WIDGET_SOURCE)
        assert _seat(["src", "--rebaseline"]) == 0
        assert tools.oo_ratchet.main(["src", "--audit-completeness"]) == 0


class TestFailClosedOnCorruptBlob:
    """A corrupt base baseline blob raises GitError — never fail-open."""

    def _commit_baseline(self, sandbox: GitSandbox, blob: str) -> str:
        sandbox.write("src/mod.py", _WIDGET_SOURCE)
        sandbox.write(".oo-baseline.json", blob)
        return sandbox.commit("corrupt baseline")

    def test_non_dict_entry_raises(self, git_sandbox: GitSandbox) -> None:
        sha = self._commit_baseline(git_sandbox, '{"src/mod.py": "not-a-dict"}\n')
        git = GitRepo(git_sandbox.root)
        with pytest.raises(GitError, match="non-dict entry"):
            git.show_baseline(sha)

    def test_non_numeric_metric_raises(self, git_sandbox: GitSandbox) -> None:
        sha = self._commit_baseline(
            git_sandbox, '{"src/mod.py": {"module_size": "big"}}\n'
        )
        git = GitRepo(git_sandbox.root)
        with pytest.raises(GitError, match="non-numeric metric"):
            git.show_baseline(sha)

    def test_bool_metric_raises(self, git_sandbox: GitSandbox) -> None:
        # A bool is an int subclass that would compare as 0/1 (fail-open).
        sha = self._commit_baseline(
            git_sandbox, '{"src/mod.py": {"future_annotations": true}}\n'
        )
        git = GitRepo(git_sandbox.root)
        with pytest.raises(GitError, match="non-numeric metric"):
            git.show_baseline(sha)

    def test_check_reports_nonzero_not_fail_open(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        sha = self._commit_baseline(git_sandbox, '{"src/mod.py": "not-a-dict"}\n')
        code = tools.oo_ratchet.main(
            ["src", "--check", "--base-ref", sha, "--require-base"]
        )
        assert code == 1
        assert "FAIL" in capsys.readouterr().out


class TestScorerRepoRelativeKeys:
    """Baseline keys are repo-relative POSIX regardless of the CLI path spelling."""

    def test_keys_are_repo_relative(
        self, git_sandbox: GitSandbox, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        git_sandbox.write("src/pkg/mod.py", _WIDGET_SOURCE)
        scorer = Scorer(git_sandbox.root / "src", git_sandbox.root)
        assert "src/pkg/mod.py" in scorer.files

    def test_ratchet_check_trivial_when_no_python_touched(
        self, git_sandbox: GitSandbox, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        git_sandbox.write("src/mod.py", _WIDGET_SOURCE)
        assert _seat(["src", "--rebaseline"]) == 0
        base = git_sandbox.commit("base")
        git_sandbox.write("README.md", "# docs only\n")
        ratchet = Ratchet(git_sandbox.root, GitRepo(git_sandbox.root))
        outcome = ratchet.check(
            Scorer(git_sandbox.root / "src", git_sandbox.root),
            base_ref=base,
            require_base=True,
        )
        assert outcome.exit_code == 0
        assert "No Python files touched" in outcome.lines[0]
