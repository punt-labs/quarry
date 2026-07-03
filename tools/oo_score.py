"""
OO Quality Score — deterministic measurement of how object-oriented a Python
module is. Produces JSON output with numeric scores for agent consumption.

Usage:
    python tools/oo_score.py <file_or_directory> [--json] [--threshold]
    python tools/oo_score.py <file_or_directory> --check     # ratchet check
    python tools/oo_score.py <file_or_directory> --verify     # baseline integrity
    python tools/oo_score.py <file_or_directory> --update    # update baseline
    python tools/oo_score.py <file_or_directory> --correct <path> --reason <text>
    python tools/oo_score.py <file_or_directory> --rebaseline # reset baseline
    python tools/oo_score.py <file_or_directory> --log       # audit history

The ratchet trusts .oo-baseline.json, but two failure modes break that trust:
a baseline can be committed out of sync with its code (a "phantom" that --check
never catches because --check only gates *touched* files), and ratio metrics
(avg_params, avg_complexity, method_ratio) regress mechanically under genuine
refactors because their denominator shifts. --verify catches phantoms; --correct
fixes one entry without a nuclear --rebaseline; ratio tolerance absorbs the
denominator artifact without loosening the absolute thresholds.

Metrics produced:
    method_ratio        % of functions that are class methods (target: >= 80%)
    encapsulation_ratio % of instance attrs with _ or __ prefix (target: 100%)
    avg_params          average parameter count excluding self/cls (target: <= 4)
    max_complexity       highest cyclomatic complexity (target: <= 10)
    avg_complexity       average cyclomatic complexity (target: <= 5)
    module_size          lines of code per module (target: <= 300)
    classes_per_module   class count per module (target: 1-3)
    class_to_func_ratio  classes / (classes + top-level functions) (target: >= 0.5)
    init_violations      count of __init__ definitions (target: 0)
    public_attr_violations count of self.X = without underscore (target: 0)
    future_annotations   whether from __future__ import annotations present (target: 1)
"""

from __future__ import annotations

import ast
import datetime
import json
import subprocess
import sys
from pathlib import Path
from typing import ClassVar, Self


def _writeln(text: str = "") -> None:
    """Write a line to stdout."""
    sys.stdout.write(text + "\n")


def _flag_value(flag: str) -> str | None:
    """Return the argv token following ``flag``, or None if absent."""
    if flag in sys.argv:
        idx = sys.argv.index(flag)
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    return None


class ModuleMetrics:
    _path: str
    _tree: ast.Module
    _source_lines: int

    def __new__(cls, path: str, source: str) -> Self:
        self = super().__new__(cls)
        self._path = path
        self._tree = ast.parse(source, filename=path)
        self._source_lines = len([line for line in source.splitlines() if line.strip()])
        return self

    def compute(self) -> dict[str, float | int | str]:
        return {
            "file": self._path,
            "module_size": self._source_lines,
            "classes_per_module": self._count_classes(),
            "top_level_functions": self._count_top_level_functions(),
            "top_level_statements": self._count_top_level_statements(),
            "method_ratio": self._method_ratio(),
            "class_to_func_ratio": self._class_to_func_ratio(),
            "encapsulation_ratio": self._encapsulation_ratio(),
            "avg_params": self._avg_params(),
            "max_complexity": self._max_complexity(),
            "avg_complexity": self._avg_complexity(),
            "init_violations": self._count_init(),
            "public_attr_violations": self._count_public_attrs(),
            "future_annotations": self._has_future_annotations(),
        }

    def _count_classes(self) -> int:
        return sum(
            1
            for node in ast.iter_child_nodes(self._tree)
            if isinstance(node, ast.ClassDef) and not self._is_type_definition(node)
        )

    @staticmethod
    def _is_type_definition(node: ast.ClassDef) -> bool:
        type_bases = {"Protocol", "TypedDict"}
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id in type_bases:
                return True
            if (
                isinstance(base, ast.Subscript)
                and isinstance(base.value, ast.Name)
                and base.value.id in type_bases
            ):
                return True
        return False

    def _count_top_level_functions(self) -> int:
        return sum(
            1
            for node in ast.iter_child_nodes(self._tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )

    def _count_top_level_statements(self) -> int:
        skip_types = (
            ast.Import,
            ast.ImportFrom,
            ast.ClassDef,
            ast.FunctionDef,
            ast.AsyncFunctionDef,
        )
        count = 0
        for node in ast.iter_child_nodes(self._tree):
            if isinstance(node, skip_types):
                continue
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                continue
            count += 1
        return count

    def _count_methods(self) -> int:
        count = 0
        for node in ast.walk(self._tree):
            if isinstance(node, ast.ClassDef):
                for item in ast.iter_child_nodes(node):
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        count += 1
        return count

    def _method_ratio(self) -> float:
        methods = self._count_methods()
        top_funcs = self._count_top_level_functions()
        total = methods + top_funcs
        if total == 0:
            top_stmts = self._count_top_level_statements()
            return 0.0 if top_stmts > 5 else 1.0
        return round(methods / total, 3)

    def _class_to_func_ratio(self) -> float:
        classes = self._count_classes()
        funcs = self._count_top_level_functions()
        total = classes + funcs
        if total == 0:
            top_stmts = self._count_top_level_statements()
            return 0.0 if top_stmts > 5 else 1.0
        return round(classes / total, 3)

    def _encapsulation_ratio(self) -> float:
        total_attrs = 0
        private_attrs = 0
        for node in ast.walk(self._tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.target is not None:
                targets = [node.target]
            for target in targets:
                if not isinstance(target, ast.Attribute):
                    continue
                if not isinstance(target.value, ast.Name):
                    continue
                if target.value.id != "self":
                    continue
                total_attrs += 1
                if target.attr.startswith("_"):
                    private_attrs += 1
        if total_attrs == 0:
            return 1.0
        return round(private_attrs / total_attrs, 3)

    def _avg_params(self) -> float:
        counts = []
        for node in ast.walk(self._tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            param_count = len(args.args) + len(args.posonlyargs) + len(args.kwonlyargs)
            if args.args and args.args[0].arg in ("self", "cls"):
                param_count -= 1
            counts.append(param_count)
        if not counts:
            return 0.0
        return round(sum(counts) / len(counts), 2)

    def _max_complexity(self) -> int:
        max_cc = 0
        for node in ast.walk(self._tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cc = self._cyclomatic_complexity(node)
                max_cc = max(max_cc, cc)
        return max_cc

    def _avg_complexity(self) -> float:
        complexities = [
            self._cyclomatic_complexity(node)
            for node in ast.walk(self._tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if not complexities:
            return 0.0
        return round(sum(complexities) / len(complexities), 2)

    @staticmethod
    def _cyclomatic_complexity(
        func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> int:
        cc = 1
        for node in ast.walk(func_node):
            if isinstance(
                node,
                (ast.If, ast.While, ast.For, ast.AsyncFor, ast.ExceptHandler),
            ):
                cc += 1
            elif isinstance(node, ast.BoolOp):
                cc += len(node.values) - 1
            elif isinstance(node, ast.Assert):
                cc += 1
            elif isinstance(node, ast.comprehension):
                cc += 1 + len(node.ifs)
        return cc

    @staticmethod
    def _has_dataclass_decorator(node: ast.ClassDef) -> bool:
        for d in node.decorator_list:
            if isinstance(d, ast.Name) and d.id == "dataclass":
                return True
            if (
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Name)
                and d.func.id == "dataclass"
            ):
                return True
            if isinstance(d, ast.Attribute) and d.attr == "dataclass":
                return True
        return False

    def _count_init(self) -> int:
        count = 0
        for node in ast.walk(self._tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if self._has_dataclass_decorator(node):
                continue
            for item in ast.iter_child_nodes(node):
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    count += 1
        return count

    def _count_public_attrs(self) -> int:
        count = 0
        for node in ast.walk(self._tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.target is not None:
                targets = [node.target]
            for target in targets:
                if not isinstance(target, ast.Attribute):
                    continue
                if not isinstance(target.value, ast.Name):
                    continue
                if target.value.id != "self":
                    continue
                if not target.attr.startswith("_"):
                    count += 1
        return count

    def _has_future_annotations(self) -> int:
        for node in ast.iter_child_nodes(self._tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "__future__"
                and any(alias.name == "annotations" for alias in node.names)
            ):
                return 1
        return 0


class Scorer:
    """Scores modules against OO quality thresholds."""

    _thresholds: dict[str, tuple[str, float]]
    _results: list[dict[str, float | int | str]]

    THRESHOLDS: ClassVar[dict[str, tuple[str, float]]] = {
        "method_ratio": (">=", 0.80),
        "encapsulation_ratio": (">=", 1.0),
        "avg_params": ("<=", 4.0),
        "max_complexity": ("<=", 10),
        "avg_complexity": ("<=", 5.0),
        "module_size": ("<=", 300),
        "classes_per_module": ("<=", 3),
        "class_to_func_ratio": (">=", 0.5),
        "init_violations": ("==", 0),
        "public_attr_violations": ("==", 0),
        "future_annotations": ("==", 1),
    }

    def __new__(cls, target: Path) -> Self:
        self = super().__new__(cls)
        self._thresholds = cls.THRESHOLDS
        if target.is_file():
            self._results = [self._score_file(target)]
        elif target.is_dir():
            self._results = self._score_directory(target)
        else:
            self._results = []
        return self

    @property
    def results(self) -> list[dict[str, float | int | str]]:
        return self._results

    @property
    def summary(self) -> dict[str, float]:
        return self._aggregate()

    @property
    def grades(self) -> dict[str, str]:
        return {
            k: "PASS" if self._check(k, v) else "FAIL" for k, v in self.summary.items()
        }

    @property
    def fail_count(self) -> int:
        return sum(1 for g in self.grades.values() if g == "FAIL")

    def _check(self, metric: str, value: float | int) -> bool:
        if metric not in self._thresholds:
            return True
        op, target = self._thresholds[metric]
        if op == ">=":
            return value >= target
        if op == "<=":
            return value <= target
        if op == "==":
            return value == target
        return True

    @staticmethod
    def _score_file(path: Path) -> dict[str, float | int | str]:
        source = path.read_text()
        return ModuleMetrics(str(path), source).compute()

    def _score_directory(self, directory: Path) -> list[dict[str, float | int | str]]:
        results: list[dict[str, float | int | str]] = []
        for py_file in sorted(directory.rglob("*.py")):
            if py_file.name.startswith("."):
                continue
            try:
                results.append(self._score_file(py_file))
            except SyntaxError as e:
                results.append({"file": str(py_file), "error": str(e)})
        return results

    def _aggregate(self) -> dict[str, float]:
        numeric_keys = list(self._thresholds)
        agg: dict[str, list[float]] = {k: [] for k in numeric_keys}
        for r in self._results:
            if "error" in r:
                continue
            for k in numeric_keys:
                if k in r:
                    agg[k].append(float(r[k]))
        summary: dict[str, float] = {}
        for k, values in agg.items():
            if not values:
                continue
            if k in (
                "max_complexity",
                "module_size",
                "classes_per_module",
                "init_violations",
                "public_attr_violations",
            ):
                summary[k] = max(values)
            elif k in ("future_annotations",):
                summary[k] = min(values)
            else:
                summary[k] = round(sum(values) / len(values), 3)
        return summary

    def print_table(self) -> None:
        summary = self.summary
        grades = self.grades
        _writeln(f"\n{'Metric':<28} {'Value':>8} {'Target':>10} {'Grade':>6}")
        _writeln("-" * 56)
        for k in self._thresholds:
            if k in summary:
                op, target_val = self._thresholds[k]
                g = grades[k]
                marker = "  " if g == "PASS" else " *"
                _writeln(f"{k:<28} {summary[k]:>8.2f} {op} {target_val:<8} {g}{marker}")

    def print_per_file(self) -> None:
        _writeln("\n--- Per-file breakdown ---")
        for r in self._results:
            _writeln(f"\n  {r.get('file', '?')}")
            for k, v in r.items():
                if k == "file":
                    continue
                if k in self._thresholds:
                    g = "PASS" if self._check(k, float(v)) else "FAIL"
                    _writeln(f"    {k:<26} {v:>8} {g}")

    def to_json(self) -> str:
        output = {
            "per_file": self._results,
            "aggregate": self.summary,
            "grades": self.grades,
            "thresholds": {k: f"{op} {v}" for k, (op, v) in self._thresholds.items()},
        }
        return json.dumps(output, indent=2)


class Ratchet:
    """Baseline persistence, regression checking, and audit logging."""

    _baseline_path: Path
    _audit_path: Path
    _baseline: dict[str, dict[str, float]]

    BASELINE_FILE: ClassVar[str] = ".oo-baseline.json"
    AUDIT_FILE: ClassVar[str] = ".oo-audit.jsonl"

    # Metrics tracked in the baseline — must match Scorer.THRESHOLDS keys.
    METRIC_KEYS: ClassVar[tuple[str, ...]] = tuple(Scorer.THRESHOLDS)

    # Float slack for --verify: recomputed rounded scores must match the
    # committed baseline exactly, so any gap beyond rounding noise is a phantom.
    VERIFY_EPSILON: ClassVar[float] = 1e-6

    def __new__(cls, root: Path | None = None) -> Self:
        self = super().__new__(cls)
        base = root if root is not None else Path.cwd()
        self._baseline_path = base / cls.BASELINE_FILE
        self._audit_path = base / cls.AUDIT_FILE
        self._baseline = self._load_baseline()
        return self

    @property
    def has_baseline(self) -> bool:
        """Return whether a baseline file exists on disk."""
        return self._baseline_path.exists()

    @property
    def baseline(self) -> dict[str, dict[str, float]]:
        return self._baseline

    # ------------------------------------------------------------------
    # Baseline I/O
    # ------------------------------------------------------------------

    def _load_baseline(self) -> dict[str, dict[str, float]]:
        if not self._baseline_path.exists():
            return {}
        text = self._baseline_path.read_text()
        raw: dict[str, dict[str, float]] = json.loads(text)
        return raw

    def _save_baseline(self, data: dict[str, dict[str, float]]) -> None:
        sorted_data = dict(sorted(data.items()))
        self._baseline_path.write_text(
            json.dumps(sorted_data, indent=2) + "\n",
        )
        self._baseline = sorted_data

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _git_commit_short() -> str | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None

    @staticmethod
    def _git_touched_files() -> list[str] | None:
        """Return repo-relative paths changed in the latest commit."""
        try:
            # Compare HEAD against its parent — works in CI (clean checkout)
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~1..HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return [line for line in result.stdout.strip().splitlines() if line]
            # HEAD~1 may not exist (initial commit) — fall through to None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None

    # ------------------------------------------------------------------
    # Metric comparison helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _meets_threshold(metric: str, value: float) -> bool:
        """Return True if value meets the absolute threshold for the metric."""
        op, target = Scorer.THRESHOLDS[metric]
        if op == ">=":
            return value >= target
        if op == "<=":
            return value <= target
        return value == target

    @staticmethod
    def _is_better_or_equal(
        metric: str,
        current: float,
        baseline_val: float,
    ) -> bool:
        """Return True if current is at least as good as baseline for the metric."""
        op, target = Scorer.THRESHOLDS[metric]
        if op == ">=":
            return current >= baseline_val
        if op == "<=":
            return current <= baseline_val
        # op == "==" — closer to target is better (or equal)
        return abs(current - target) <= abs(baseline_val - target)

    @staticmethod
    def _is_strictly_better(
        metric: str,
        current: float,
        baseline_val: float,
    ) -> bool:
        """Return True if current is strictly better than baseline."""
        op, target = Scorer.THRESHOLDS[metric]
        if op == ">=":
            return current > baseline_val
        if op == "<=":
            return current < baseline_val
        # op == "==" — strictly closer to target
        return abs(current - target) < abs(baseline_val - target)

    # ------------------------------------------------------------------
    # Extract per-file metric dicts from Scorer results
    # ------------------------------------------------------------------

    @staticmethod
    def _results_by_file(
        results: list[dict[str, float | int | str]],
    ) -> dict[str, dict[str, float]]:
        """Build {repo_relative_path: {metric: value}} from scorer results."""
        out: dict[str, dict[str, float]] = {}
        for r in results:
            if "error" in r:
                continue
            fpath = str(r["file"])
            metrics: dict[str, float] = {}
            for k in Ratchet.METRIC_KEYS:
                if k in r:
                    metrics[k] = float(r[k])
            out[fpath] = metrics
        return out

    # ------------------------------------------------------------------
    # --check
    # ------------------------------------------------------------------

    def check(self, scorer: Scorer) -> int:
        """Compare touched files against baseline. Return exit code."""
        if not self.has_baseline:
            _writeln("No baseline -- run --update to create one")
            return 0

        current_by_file = self._results_by_file(scorer.results)

        # Determine which files are "touched"
        git_touched = self._git_touched_files()
        scored_files = set(current_by_file)

        if git_touched is not None:
            touched = scored_files & set(git_touched)
        else:
            # Git unavailable — compare all scored files against baseline
            touched = scored_files

        # Filter to only Python files
        touched = {f for f in touched if f.endswith(".py")}

        if not touched:
            _writeln("No Python files touched -- trivial pass")
            return 0

        any_regression = False
        any_improvement = False
        rows: list[tuple[str, str, str, str, str, str]] = []

        for fpath in sorted(touched):
            current = current_by_file.get(fpath)
            if current is None:
                continue
            baseline_entry = self._baseline.get(fpath)

            if baseline_entry is None:
                # New file — check against absolute thresholds only
                all_passed = True
                for metric in self.METRIC_KEYS:
                    if metric not in current:
                        continue
                    val = current[metric]
                    passed = self._meets_threshold(metric, val)
                    grade = "PASS" if passed else "FAIL"
                    rows.append((fpath, metric, "NEW", f"{val:.3f}", "--", grade))
                    if not passed:
                        any_regression = True
                        all_passed = False
                # A new file that passes all thresholds counts as improvement
                if all_passed:
                    any_improvement = True
                continue

            for metric in self.METRIC_KEYS:
                if metric not in current or metric not in baseline_entry:
                    continue
                cur_val = current[metric]
                base_val = baseline_entry[metric]
                delta = cur_val - base_val

                if self._is_strictly_better(metric, cur_val, base_val):
                    grade = "IMPROVED"
                    any_improvement = True
                elif self._is_better_or_equal(metric, cur_val, base_val):
                    grade = "PASS"
                else:
                    grade = "REGRESSED"
                    any_regression = True

                if delta != 0.0 or grade == "REGRESSED":
                    rows.append(
                        (
                            fpath,
                            metric,
                            f"{base_val:.3f}",
                            f"{cur_val:.3f}",
                            f"{delta:+.3f}",
                            grade,
                        )
                    )

        # Print comparison table
        _writeln(
            f"\n{'File':<40} {'Metric':<26} {'Baseline':>10} "
            f"{'Current':>10} {'Delta':>8} {'Status':>10}",
        )
        _writeln("-" * 108)
        for row in rows:
            _writeln(
                f"{row[0]:<40} {row[1]:<26} {row[2]:>10} "
                f"{row[3]:>10} {row[4]:>8} {row[5]:>10}",
            )

        if not rows:
            _writeln("  (all metrics unchanged -- baseline is current)")
            return 0

        if any_regression:
            _writeln("\nFAIL: regression detected")
            return 1
        if not any_improvement:
            _writeln("\nFAIL: no metric improved on any touched file")
            return 1

        _writeln("\nPASS: at least one metric improved, no regressions")
        return 0

    # ------------------------------------------------------------------
    # --verify
    # ------------------------------------------------------------------

    def verify(self, scorer: Scorer) -> int:
        """Fail if any baseline entry diverges from the committed code's score.

        Distinct from --check: --check gates *touched* files against the
        baseline to block regressions, while --verify asserts the *entire*
        committed baseline faithfully matches the current source. A phantom —
        a baseline entry committed out of sync with its code — is caught here
        at PR time before it can sit dormant. In CI the working tree is the
        committed checkout, so scoring it and comparing against the committed
        baseline is exactly "does the baseline match the code?".
        """
        if not self.has_baseline:
            _writeln("No baseline -- run --update to create one")
            return 0

        current_by_file = self._results_by_file(scorer.results)
        rows = self._integrity_rows(current_by_file)

        if not rows:
            _writeln("\nPASS: baseline matches committed code")
            return 0

        _writeln(
            f"\n{'File':<40} {'Metric':<24} {'Baseline':>10} "
            f"{'Current':>10} {'Issue':>10}",
        )
        _writeln("-" * 98)
        for fpath, metric, base_s, cur_s, issue in rows:
            _writeln(f"{fpath:<40} {metric:<24} {base_s:>10} {cur_s:>10} {issue:>10}")

        _writeln("\nFAIL: baseline diverges from committed code")
        _writeln(
            "  fix a proven phantom with: "
            "python tools/oo_score.py <dir> --correct <file> --reason <why>",
        )
        return 1

    def _integrity_rows(
        self,
        current_by_file: dict[str, dict[str, float]],
    ) -> list[tuple[str, str, str, str, str]]:
        """Return (file, metric, baseline, current, issue) rows for divergences."""
        baseline_files = set(self._baseline)
        scored_files = set(current_by_file)

        stale = [
            (fpath, "(file)", "present", "absent", "stale")
            for fpath in sorted(baseline_files - scored_files)
        ]
        unrecorded = [
            (fpath, "(file)", "absent", "present", "unrecorded")
            for fpath in sorted(scored_files - baseline_files)
        ]
        phantom = [
            (
                fpath,
                metric,
                f"{self._baseline[fpath][metric]:.3f}",
                f"{current_by_file[fpath][metric]:.3f}",
                "phantom",
            )
            for fpath in sorted(baseline_files & scored_files)
            for metric in self.METRIC_KEYS
            if metric in self._baseline[fpath]
            and metric in current_by_file[fpath]
            and abs(self._baseline[fpath][metric] - current_by_file[fpath][metric])
            > self.VERIFY_EPSILON
        ]
        return stale + unrecorded + phantom

    # ------------------------------------------------------------------
    # --update
    # ------------------------------------------------------------------

    def update(self, scorer: Scorer) -> int:
        """Update baseline for files that did not regress. Return exit code."""
        current_by_file = self._results_by_file(scorer.results)
        new_baseline = dict(self._baseline)
        refused: list[tuple[str, str]] = []
        updated_count = 0
        added_count = 0

        # Track deltas for audit
        deltas: dict[str, dict[str, list[float]]] = {}

        for fpath in sorted(current_by_file):
            current = current_by_file[fpath]
            baseline_entry = self._baseline.get(fpath)

            if baseline_entry is None:
                # New file — add unconditionally
                new_baseline[fpath] = current
                added_count += 1
                # Record all metrics as deltas for new files
                file_deltas: dict[str, list[float]] = {}
                for metric in self.METRIC_KEYS:
                    if metric in current:
                        file_deltas[metric] = [0.0, current[metric]]
                if file_deltas:
                    deltas[fpath] = file_deltas
                continue

            # Check for regressions
            has_regression = False
            for metric in self.METRIC_KEYS:
                if metric not in current or metric not in baseline_entry:
                    continue
                cur_val = current[metric]
                base_val = baseline_entry[metric]
                if not self._is_better_or_equal(metric, cur_val, base_val):
                    refused.append((fpath, metric))
                    has_regression = True

            if has_regression:
                continue

            # No regression — update and record deltas
            file_deltas = {}
            for metric in self.METRIC_KEYS:
                if metric not in current or metric not in baseline_entry:
                    continue
                if current[metric] != baseline_entry[metric]:
                    file_deltas[metric] = [baseline_entry[metric], current[metric]]

            new_baseline[fpath] = current
            updated_count += 1
            if file_deltas:
                deltas[fpath] = file_deltas

        # Remove deleted files (in baseline but not on disk)
        removed_count = 0
        for fpath in list(new_baseline):
            if fpath not in current_by_file:
                del new_baseline[fpath]
                removed_count += 1

        self._save_baseline(new_baseline)

        # Count improved files (files where at least one metric changed for the better)
        files_improved = sum(1 for d in deltas.values() if d)

        # Append audit log
        self._append_audit(
            files_scored=len(current_by_file),
            files_improved=files_improved,
            files_regressed=len({f for f, _ in refused}),
            verdict="pass" if not refused else "fail",
            deltas=deltas,
        )

        # Report
        _writeln(f"\nBaseline updated: {self._baseline_path}")
        _writeln(f"  files scored:  {len(current_by_file)}")
        _writeln(f"  files added:   {added_count}")
        _writeln(f"  files updated: {updated_count}")
        _writeln(f"  files removed: {removed_count}")

        if refused:
            _writeln(f"\n  REFUSED ({len({f for f, _ in refused})} files):")
            for fpath, metric in refused:
                _writeln(f"    {fpath}: {metric} regressed")
            return 1

        return 0

    # ------------------------------------------------------------------
    # --rebaseline
    # ------------------------------------------------------------------

    def rebaseline(self, scorer: Scorer) -> int:
        """Unconditionally reset the baseline to current scores."""
        current_by_file = self._results_by_file(scorer.results)
        self._save_baseline(current_by_file)
        self._append_audit(
            files_scored=len(current_by_file),
            files_improved=0,
            files_regressed=0,
            verdict="rebaseline",
            deltas={},
        )
        _writeln(f"\nBaseline reset: {self._baseline_path}")
        _writeln(f"  files scored: {len(current_by_file)}")
        return 0

    # ------------------------------------------------------------------
    # --correct
    # ------------------------------------------------------------------

    def correct(self, target_file: str, reason: str) -> int:
        """Re-record ONE file's baseline entry to its true current score.

        Fixes a proven phantom (a baseline out of sync with committed code)
        without the nuclear full --rebaseline that resets every file.

        Guardrail: a non-empty reason is mandatory and is written to the audit
        log under a distinct "correct" verdict. This is NOT a way to bless a
        real code regression — the single-file scope and the audited reason
        make every correction visible and reviewable in the PR diff, exactly
        where a laundered regression would be caught. Use it only when --verify
        reports a phantom, never to silence --check.
        """
        if not reason.strip():
            _writeln("--correct requires a non-empty --reason")
            return 2

        path = Path(target_file)
        if not path.is_file():
            _writeln(f"Not a file: {target_file}")
            return 2

        try:
            scored = self._results_by_file([Scorer._score_file(path)])
        except SyntaxError as exc:
            _writeln(f"Cannot score {target_file}: {exc}")
            return 2

        key = str(path)
        true_metrics = scored.get(key)
        if true_metrics is None:
            _writeln(f"No metrics produced for {target_file}")
            return 2

        old_entry = self._baseline.get(key)
        file_deltas = {
            metric: [old_entry[metric], true_metrics[metric]]
            for metric in self.METRIC_KEYS
            if old_entry is not None
            and metric in old_entry
            and metric in true_metrics
            and old_entry[metric] != true_metrics[metric]
        }

        new_baseline = dict(self._baseline)
        new_baseline[key] = true_metrics
        self._save_baseline(new_baseline)

        self._append_audit(
            files_scored=1,
            files_improved=0,
            files_regressed=0,
            verdict="correct",
            deltas={key: file_deltas} if file_deltas else {},
            reason=reason,
        )

        _writeln(f"\nCorrected baseline entry: {key}")
        _writeln(f"  reason: {reason}")
        if file_deltas:
            for metric, (before, after) in sorted(file_deltas.items()):
                _writeln(f"    {metric}: {before} -> {after}")
        else:
            _writeln("  (entry added — no prior value)")
        return 0

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def _append_audit(
        self,
        *,
        files_scored: int,
        files_improved: int,
        files_regressed: int,
        verdict: str,
        deltas: dict[str, dict[str, list[float]]],
        reason: str | None = None,
    ) -> None:
        commit = self._git_commit_short()
        entry: dict[str, object] = {
            "ts": datetime.datetime.now(datetime.UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ",
            ),
            "commit": commit,
            "files_scored": files_scored,
            "files_improved": files_improved,
            "files_regressed": files_regressed,
            "verdict": verdict,
            "deltas": deltas,
        }
        if reason is not None:
            entry["reason"] = reason
        with self._audit_path.open("a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")

    # ------------------------------------------------------------------
    # --log
    # ------------------------------------------------------------------

    def show_log(self) -> int:
        """Print audit history. Return exit code."""
        if not self._audit_path.exists():
            _writeln("No audit log found")
            return 0

        _writeln(
            f"\n{'Timestamp':<22} {'Commit':<10} {'Scored':>7} "
            f"{'Improved':>9} {'Regressed':>10} {'Verdict':>8}",
        )
        _writeln("-" * 70)

        for line in self._audit_path.read_text().splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            _writeln(
                f"{entry['ts']:<22} {entry.get('commit') or '?'!s:<10} "
                f"{entry['files_scored']:>7} {entry['files_improved']:>9} "
                f"{entry['files_regressed']:>10} {entry['verdict']:>8}",
            )
        return 0


def main() -> None:
    if len(sys.argv) < 2:
        _writeln(
            f"Usage: {sys.argv[0]} <file_or_directory> "
            f"[--json] [--threshold] [--check] [--verify] [--update] "
            f"[--correct <file> --reason <text>] [--rebaseline] [--log]",
        )
        sys.exit(1)

    target = Path(sys.argv[1])
    if not target.exists():
        _writeln(f"Not found: {target}")
        sys.exit(1)

    ratchet = Ratchet()

    if "--correct" in sys.argv:
        target_file = _flag_value("--correct")
        reason = _flag_value("--reason")
        if target_file is None:
            _writeln("--correct requires a file path")
            sys.exit(2)
        if reason is None:
            _writeln("--correct requires --reason <text>")
            sys.exit(2)
        sys.exit(ratchet.correct(target_file, reason))

    scorer = Scorer(target)

    if "--check" in sys.argv:
        sys.exit(ratchet.check(scorer))
    elif "--verify" in sys.argv:
        sys.exit(ratchet.verify(scorer))
    elif "--rebaseline" in sys.argv:
        sys.exit(ratchet.rebaseline(scorer))
    elif "--update" in sys.argv:
        sys.exit(ratchet.update(scorer))
    elif "--log" in sys.argv:
        sys.exit(ratchet.show_log())
    elif "--json" in sys.argv:
        _writeln(scorer.to_json())
    else:
        scorer.print_table()
        if "--threshold" in sys.argv:
            scorer.print_per_file()

    sys.exit(1 if scorer.fail_count > 0 else 0)


if __name__ == "__main__":
    main()
