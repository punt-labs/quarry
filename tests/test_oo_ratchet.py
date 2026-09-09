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
    SHAPE_METRICS,
    Diff,
    ExtractionDetector,
    GitError,
    GitRepo,
    LineChange,
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


# --- extraction provenance: honest extraction vs. new debt (quarry-p50s) ---

# A baselined "god module" of free functions -- already tracked, so its own
# method_ratio/class_to_func_ratio only ever have to not regress vs base.
_GOD_MODULE_SOURCE = '''\
"""God module of report helpers."""

from __future__ import annotations


def format_header(title: str) -> str:
    return f"== {title} =="


def format_footer(note: str) -> str:
    return f"-- {note} --"


def format_body(text: str) -> str:
    return text.strip()


def format_summary(total: int) -> str:
    return f"total: {total}"
'''

# HEAD: format_header/format_footer removed from the god module -- module_size
# shrinks (an IMPROVEMENT), satisfying the S5 improvement gate on its own.
_GOD_MODULE_AFTER_EXTRACTION = '''\
"""God module of report helpers."""

from __future__ import annotations


def format_body(text: str) -> str:
    return text.strip()


def format_summary(total: int) -> str:
    return f"total: {total}"
'''

# HEAD: format_header/format_footer moved here verbatim -- a genuine
# extraction. Zero classes, so method_ratio/class_to_func_ratio would FAIL
# the absolute new-file bar without the provenance carve-out.
_EXTRACTED_MODULE_SOURCE = """\
from __future__ import annotations


def format_header(title: str) -> str:
    return f"== {title} =="


def format_footer(note: str) -> str:
    return f"-- {note} --"
"""

# A wholly new zero-class module that shares NO content with anything
# removed elsewhere in the diff -- must be scored as genuinely new, not
# waived, even though it too has zero classes.
_UNRELATED_NEW_MODULE_SOURCE = """\
from __future__ import annotations


def compute_total(values: list[int]) -> int:
    return sum(values)


def compute_average(values: list[int]) -> float:
    return sum(values) / len(values)
"""

# A brand-new zero-class module with fat parameters -- must fail on
# avg_params (outside SHAPE_METRICS) even when it IS a genuine extraction of
# something else, proving the carve-out never touches non-shape metrics.
_FAT_PARAM_EXTRACTED_SOURCE = """\
from __future__ import annotations


def format_header(title: str) -> str:
    return f"== {title} =="


def format_footer(note: str) -> str:
    return f"-- {note} --"


def build_report(
    a: int,
    b: int,
    c: int,
    d: int,
    e: int,
    f: int,
    g: int,
    h: int,
    i: int,
    j: int,
    k: int,
    l: int,
    m: int,
    n: int,
) -> int:
    return a + b + c + d + e + f + g + h + i + j + k + l + m + n
"""


class TestExtractionProvenance:
    """A new zero-class file scores relative to its origin, never in a vacuum.

    Only method_ratio/class_to_func_ratio are ever waived, and only when the
    new file's content demonstrably traces to a baselined origin touched in
    the same diff (``provenance.ExtractionDetector``). Every other metric,
    and every new file without proven provenance, keeps the full absolute
    bar -- the fix must never launder genuinely new debt.
    """

    def test_honest_extraction_from_baselined_origin_passes(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        git_sandbox.write("src/mod.py", _GOD_MODULE_SOURCE)
        assert _seat(["src", "--rebaseline"]) == 0
        base = git_sandbox.commit("base")
        git_sandbox.write("src/mod.py", _GOD_MODULE_AFTER_EXTRACTION)
        git_sandbox.write("src/extracted.py", _EXTRACTED_MODULE_SOURCE)
        # A brand-new path is invisible to `git diff <base>` until staged --
        # unlike a tracked file's edits, git has no committed blob to diff an
        # untracked file against, so it never appears in the touched set.
        git_sandbox.run_git("add", "-A")
        assert _seat(["src", "--rebaseline"]) == 0
        code = _check(base)
        out = capsys.readouterr().out
        assert code == 0, out
        assert "EXTRACTED" in out
        assert "regression detected" not in out

    def test_unrelated_new_zero_class_file_without_provenance_fails(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        git_sandbox.write("src/mod.py", _GOD_MODULE_SOURCE)
        assert _seat(["src", "--rebaseline"]) == 0
        base = git_sandbox.commit("base")
        # mod.py is untouched at HEAD -- only a brand-new, unrelated module
        # appears. Its content shares nothing with any removed line, so no
        # origin explains it: the carve-out must not fire.
        git_sandbox.write("src/unrelated.py", _UNRELATED_NEW_MODULE_SOURCE)
        git_sandbox.run_git("add", "-A")
        assert _seat(["src", "--rebaseline"]) == 0
        code = _check(base)
        out = capsys.readouterr().out
        assert code == 1
        assert "regression detected" in out
        assert "method_ratio" in out
        assert "class_to_func_ratio" in out

    def test_fat_parameter_module_fails_even_when_extracted(
        self,
        git_sandbox: GitSandbox,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(git_sandbox.root)
        git_sandbox.write("src/mod.py", _GOD_MODULE_SOURCE)
        assert _seat(["src", "--rebaseline"]) == 0
        base = git_sandbox.commit("base")
        git_sandbox.write("src/mod.py", _GOD_MODULE_AFTER_EXTRACTION)
        # Genuine extraction (format_header/format_footer moved verbatim)
        # PLUS a brand-new fat-parameter function riding along in the same
        # file -- avg_params must still fail; the carve-out never covers it.
        git_sandbox.write("src/extracted.py", _FAT_PARAM_EXTRACTED_SOURCE)
        git_sandbox.run_git("add", "-A")
        assert _seat(["src", "--rebaseline"]) == 0
        code = _check(base)
        out = capsys.readouterr().out
        assert code == 1
        assert "regression detected" in out
        assert "avg_params" in out


class TestExtractionDetector:
    """Unit coverage for the pure overlap-detection logic (no git)."""

    def test_origin_of_picks_the_best_matching_origin(self) -> None:
        origin_a = frozenset({"def keep_this_one(x: int) -> int:", "return x + 1"})
        origin_b = frozenset({"def totally_unrelated() -> None:", "pass"})
        detector = ExtractionDetector({"a.py": origin_a, "b.py": origin_b})
        new_lines = frozenset({"def keep_this_one(x: int) -> int:", "return x + 1"})
        assert detector.origin_of(new_lines) == "a.py"

    def test_origin_of_returns_none_below_threshold(self) -> None:
        origin = frozenset({"def unrelated_thing() -> None:", "return None"})
        detector = ExtractionDetector({"a.py": origin})
        new_lines = frozenset({"def brand_new_thing() -> int:", "return 42"})
        assert detector.origin_of(new_lines) is None

    def test_origin_of_returns_none_for_empty_new_lines(self) -> None:
        detector = ExtractionDetector({"a.py": frozenset({"def f() -> None:"})})
        assert detector.origin_of(frozenset()) is None

    def test_substantive_lines_filters_short_and_blank_lines(self) -> None:
        lines = ["", "   ", ")", "def real_function_name() -> None:", "  x  "]
        result = ExtractionDetector.substantive_lines(lines)
        assert result == {"def real_function_name() -> None:"}

    def test_shape_metrics_is_exactly_the_two_class_shape_metrics(self) -> None:
        assert {"method_ratio", "class_to_func_ratio"} == SHAPE_METRICS


class TestLineChangeParsing:
    """Unit coverage for ``LineChange.parse_patch`` against a synthetic patch."""

    def test_parses_added_and_removed_lines_per_path(self) -> None:
        patch = "\n".join(
            [
                "diff --git a/src/mod.py b/src/mod.py",
                "index 1111111..2222222 100644",
                "--- a/src/mod.py",
                "+++ b/src/mod.py",
                "@@ -1,2 +1,1 @@",
                "-def old_func():",
                "-    return 1",
                "+def new_func():",
            ]
        )
        result = LineChange.parse_patch(patch)
        assert result["src/mod.py"].removed == frozenset(
            {"def old_func():", "    return 1"}
        )
        assert result["src/mod.py"].added == frozenset({"def new_func():"})

    def test_new_file_has_only_added_lines(self) -> None:
        patch = "\n".join(
            [
                "diff --git a/src/new.py b/src/new.py",
                "new file mode 100644",
                "index 0000000..3333333",
                "--- /dev/null",
                "+++ b/src/new.py",
                "@@ -0,0 +1,1 @@",
                "+def f():",
            ]
        )
        result = LineChange.parse_patch(patch)
        assert result["src/new.py"].removed == frozenset()
        assert result["src/new.py"].added == frozenset({"def f():"})

    def test_empty_patch_yields_no_paths(self) -> None:
        assert LineChange.parse_patch("") == {}


class TestDiffFromNameStatus:
    """Unit coverage for ``Diff.from_name_status`` against synthetic output."""

    def test_parses_modified_and_added_files(self) -> None:
        out = "M\tsrc/mod.py\nA\tsrc/new.py\n"
        result = Diff.from_name_status(out)
        assert result.touched == frozenset({"src/mod.py", "src/new.py"})
        assert result.renames == {}

    def test_parses_rename_with_provenance(self) -> None:
        out = "R100\tsrc/old.py\tsrc/new.py\n"
        result = Diff.from_name_status(out)
        assert result.touched == frozenset({"src/new.py"})
        assert result.renames == {"src/new.py": "src/old.py"}

    def test_blank_lines_are_skipped(self) -> None:
        out = "M\tsrc/mod.py\n\n"
        result = Diff.from_name_status(out)
        assert result.touched == frozenset({"src/mod.py"})
