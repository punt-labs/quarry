"""
OO Quality Score — deterministic measurement of how object-oriented a Python
module is. Produces JSON output with numeric scores for agent consumption.

Usage:
    python tools/oo_score.py <file_or_directory> [--json] [--threshold]

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
import json
import sys
from pathlib import Path
from typing import Self


class ModuleMetrics:
    _path: str
    _tree: ast.Module
    _source_lines: int

    def __new__(cls, path: str, source: str) -> Self:
        self = super().__new__(cls)
        self._path = path
        self._tree = ast.parse(source, filename=path)
        self._source_lines = len([l for l in source.splitlines() if l.strip()])
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
            1 for node in ast.iter_child_nodes(self._tree)
            if isinstance(node, ast.ClassDef)
            and not self._is_type_definition(node)
        )

    @staticmethod
    def _is_type_definition(node: ast.ClassDef) -> bool:
        type_bases = {"Protocol", "TypedDict"}
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id in type_bases:
                return True
            if isinstance(base, ast.Subscript) and isinstance(base.value, ast.Name) and base.value.id in type_bases:
                return True
        return False

    def _count_top_level_functions(self) -> int:
        return sum(
            1 for node in ast.iter_child_nodes(self._tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )

    def _count_top_level_statements(self) -> int:
        skip_types = (
            ast.Import, ast.ImportFrom, ast.ClassDef,
            ast.FunctionDef, ast.AsyncFunctionDef,
        )
        count = 0
        for node in ast.iter_child_nodes(self._tree):
            if isinstance(node, skip_types):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
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
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
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
        complexities = []
        for node in ast.walk(self._tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                complexities.append(self._cyclomatic_complexity(node))
        if not complexities:
            return 0.0
        return round(sum(complexities) / len(complexities), 2)

    @staticmethod
    def _cyclomatic_complexity(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
        cc = 1
        for node in ast.walk(func_node):
            if isinstance(node, (ast.If, ast.While, ast.For, ast.AsyncFor)):
                cc += 1
            elif isinstance(node, ast.ExceptHandler):
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
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "dataclass":
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
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
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
            if isinstance(node, ast.ImportFrom):
                if node.module == "__future__" and any(
                    alias.name == "annotations" for alias in node.names
                ):
                    return 1
        return 0


class Scorer:
    """Scores modules against OO quality thresholds."""

    _thresholds: dict[str, tuple[str, float]]
    _results: list[dict[str, float | int | str]]

    THRESHOLDS: dict[str, tuple[str, float]] = {
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
            k: "PASS" if self._check(k, v) else "FAIL"
            for k, v in self.summary.items()
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
            if k in ("max_complexity", "module_size", "init_violations", "public_attr_violations"):
                summary[k] = max(values)
            elif k in ("future_annotations",):
                summary[k] = min(values)
            else:
                summary[k] = round(sum(values) / len(values), 3)
        return summary

    def print_table(self) -> None:
        summary = self.summary
        grades = self.grades
        print(f"\n{'Metric':<28} {'Value':>8} {'Target':>10} {'Grade':>6}")
        print("-" * 56)
        for k in self._thresholds:
            if k in summary:
                op, target_val = self._thresholds[k]
                g = grades[k]
                marker = "  " if g == "PASS" else " *"
                print(f"{k:<28} {summary[k]:>8.2f} {op} {target_val:<8} {g}{marker}")

    def print_per_file(self) -> None:
        print("\n--- Per-file breakdown ---")
        for r in self._results:
            print(f"\n  {r.get('file', '?')}")
            for k, v in r.items():
                if k == "file":
                    continue
                if k in self._thresholds:
                    g = "PASS" if self._check(k, float(v)) else "FAIL"
                    print(f"    {k:<26} {v:>8} {g}")

    def to_json(self) -> str:
        output = {
            "per_file": self._results,
            "aggregate": self.summary,
            "grades": self.grades,
            "thresholds": {k: f"{op} {v}" for k, (op, v) in self._thresholds.items()},
        }
        return json.dumps(output, indent=2)


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <file_or_directory> [--json] [--threshold]")
        sys.exit(1)

    target = Path(sys.argv[1])
    if not target.exists():
        print(f"Not found: {target}")
        sys.exit(1)

    scorer = Scorer(target)

    if "--json" in sys.argv:
        print(scorer.to_json())
    else:
        scorer.print_table()
        if "--threshold" in sys.argv:
            scorer.print_per_file()

    sys.exit(1 if scorer.fail_count > 0 else 0)


if __name__ == "__main__":
    main()
