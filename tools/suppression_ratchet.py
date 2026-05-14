"""Count lint/type suppressions in a Python codebase and enforce a ratchet."""

from __future__ import annotations

import ast
import datetime
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import ClassVar, Self


def _writeln(text: str = "") -> None:
    """Write a line to stdout."""
    sys.stdout.write(text + "\n")


_NOQA_RE = re.compile(r"#\s*noqa\b")
_TYPE_IGNORE_RE = re.compile(r"#\s*type:\s*ignore\b")
_PYLINT_DISABLE_RE = re.compile(r"#\s*pylint:\s*disable\b")
_PYRIGHT_IGNORE_RE = re.compile(r"#\s*pyright:\s*ignore\b")

_CODE_START_RE = re.compile(
    r"^(?:[a-zA-Z_]\w*\s*[=:([]|"
    r"(?:def|class|return|yield|raise|import|from|if|elif|else|for|while|"
    r"try|except|finally|with|assert|del|pass|break|continue|global|nonlocal)\b|@)",
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("noqa", _NOQA_RE),
    ("type_ignore", _TYPE_IGNORE_RE),
    ("pylint_disable", _PYLINT_DISABLE_RE),
    ("pyright_ignore", _PYRIGHT_IGNORE_RE),
)

_CATEGORIES: tuple[str, ...] = (
    "noqa",
    "type_ignore",
    "pylint_disable",
    "pyright_ignore",
    "per_file_ignores",
)


class FileSuppressions:
    """Count suppression comments in a single Python source file."""

    _path: str
    _counts: dict[str, int]

    def __new__(cls, path: str, source: str) -> Self:
        self = super().__new__(cls)
        self._path = path
        self._counts = {name: 0 for name, _ in _PATTERNS}
        self._scan(source)
        return self

    @property
    def path(self) -> str:
        return self._path

    @property
    def noqa(self) -> int:
        return self._counts["noqa"]

    @property
    def type_ignore(self) -> int:
        return self._counts["type_ignore"]

    @property
    def pylint_disable(self) -> int:
        return self._counts["pylint_disable"]

    @property
    def pyright_ignore(self) -> int:
        return self._counts["pyright_ignore"]

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    def _scan(self, source: str) -> None:
        """Scan source for suppression comments on code lines."""
        for line in self._code_lines(source):
            for name, pattern in _PATTERNS:
                if pattern.search(line):
                    self._counts[name] += 1

    def _code_lines(self, source: str) -> list[str]:
        """Return lines carrying code, excluding comments and string interiors."""
        lines = source.splitlines()
        if not lines:
            return []
        string_lines = self._string_line_numbers(source)
        return [
            line
            for i, line in enumerate(lines, start=1)
            if self._is_code_line(line, i, string_lines)
        ]

    @staticmethod
    def _string_line_numbers(source: str) -> set[int]:
        """Return 1-based line numbers that fall inside string literals."""
        result: set[int] = set()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return result
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.end_lineno is not None
            ):
                result.update(range(node.lineno, node.end_lineno + 1))
        return result

    @staticmethod
    def _is_code_line(line: str, lineno: int, string_lines: set[int]) -> bool:
        """Determine whether a source line carries actual code."""
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            return False
        if lineno not in string_lines:
            return True
        # Line overlaps a string literal — check if it has code structure
        if stripped.startswith(('"""', "'''")):
            return True
        return bool(_CODE_START_RE.match(stripped))

    def to_dict(self) -> dict[str, int]:
        """Return non-zero category counts."""
        return {k: v for k, v in self._counts.items() if v}


class PerFileIgnoresCounter:
    """Count rule codes in [tool.ruff.lint.per-file-ignores]."""

    _total: int
    _breakdown: dict[str, int]

    def __new__(cls, pyproject_path: Path) -> Self:
        self = super().__new__(cls)
        self._total = 0
        self._breakdown = {}
        self._parse(pyproject_path)
        return self

    @property
    def total(self) -> int:
        return self._total

    @property
    def breakdown(self) -> dict[str, int]:
        return dict(self._breakdown)

    def _parse(self, pyproject_path: Path) -> None:
        """Parse pyproject.toml and count per-file-ignores rule codes."""
        if not pyproject_path.exists():
            return
        try:
            data = tomllib.loads(pyproject_path.read_text())
        except (tomllib.TOMLDecodeError, OSError):
            return
        ignores = (
            data.get("tool", {})
            .get("ruff", {})
            .get("lint", {})
            .get("per-file-ignores", {})
        )
        if not isinstance(ignores, dict):
            return
        for pattern, codes in ignores.items():
            if isinstance(codes, list):
                count = len(codes)
                self._breakdown[pattern] = count
                self._total += count


class SuppressionReport:
    """Aggregate suppression counts across files."""

    _total: int
    _by_category: dict[str, int]
    _by_file: dict[str, dict[str, int]]

    def __new__(
        cls,
        file_results: list[FileSuppressions],
        per_file_ignores_count: int,
    ) -> Self:
        self = super().__new__(cls)
        self._by_category = dict.fromkeys(_CATEGORIES, 0)
        self._by_category["per_file_ignores"] = per_file_ignores_count
        self._by_file = {}
        for fs in file_results:
            for name, _ in _PATTERNS:
                self._by_category[name] += getattr(fs, name)
            if fs.total > 0:
                self._by_file[fs.path] = fs.to_dict()
        self._total = sum(self._by_category.values())
        return self

    @property
    def total(self) -> int:
        return self._total

    @property
    def by_category(self) -> dict[str, int]:
        return dict(self._by_category)

    @property
    def by_file(self) -> dict[str, dict[str, int]]:
        return dict(self._by_file)

    def to_json(self) -> str:
        """Return machine-readable JSON."""
        return json.dumps(
            {
                "total": self._total,
                "by_category": self._by_category,
                "by_file": self._by_file,
            },
            indent=2,
        )

    def print_report(self) -> None:
        """Print human-readable summary."""
        _writeln(f"\nTotal suppressions: {self._total}")
        _writeln(f"\n{'Category':<20} {'Count':>6}")
        _writeln("-" * 28)
        for category, count in sorted(self._by_category.items()):
            _writeln(f"{category:<20} {count:>6}")

    def print_threshold(self) -> None:
        """Print per-file breakdown."""
        _writeln("\n--- Per-file breakdown ---")
        if not self._by_file:
            _writeln("  (no suppressions found)")
            return
        for fpath in sorted(self._by_file):
            counts = self._by_file[fpath]
            file_total = sum(counts.values())
            _writeln(f"\n  {fpath}  (total: {file_total})")
            for cat, count in sorted(counts.items()):
                _writeln(f"    {cat:<20} {count:>4}")


class Scanner:
    """Scan a directory for suppression comments."""

    _report: SuppressionReport

    def __new__(cls, target: Path, project_root: Path | None = None) -> Self:
        self = super().__new__(cls)
        root = project_root if project_root is not None else Path.cwd()
        file_results = self._collect_files(target)
        pfi = PerFileIgnoresCounter(root / "pyproject.toml")
        self._report = SuppressionReport(file_results, pfi.total)
        return self

    @property
    def report(self) -> SuppressionReport:
        return self._report

    @staticmethod
    def _collect_files(target: Path) -> list[FileSuppressions]:
        """Discover and scan Python files."""
        results: list[FileSuppressions] = []
        if target.is_file():
            results.append(FileSuppressions(str(target), target.read_text()))
            return results
        if not target.is_dir():
            return results
        for py_file in sorted(target.rglob("*.py")):
            if py_file.name.startswith("."):
                continue
            try:
                source = py_file.read_text()
            except OSError:
                continue
            results.append(FileSuppressions(str(py_file), source))
        return results


class Baseline:
    """Baseline persistence, regression checking, and audit logging."""

    _baseline_path: Path
    _audit_path: Path

    BASELINE_FILE: ClassVar[str] = ".suppression-baseline.json"
    AUDIT_FILE: ClassVar[str] = ".suppression-audit.jsonl"

    def __new__(cls, root: Path | None = None) -> Self:
        self = super().__new__(cls)
        base = root if root is not None else Path.cwd()
        self._baseline_path = base / cls.BASELINE_FILE
        self._audit_path = base / cls.AUDIT_FILE
        return self

    @property
    def has_baseline(self) -> bool:
        return self._baseline_path.exists()

    def _load_baseline(self) -> dict[str, object]:
        if not self._baseline_path.exists():
            return {}
        raw = json.loads(self._baseline_path.read_text())
        result: dict[str, object] = dict(raw)
        return result

    def _save_baseline(self, report: SuppressionReport) -> None:
        data = {
            "total": report.total,
            "by_category": report.by_category,
            "by_file": report.by_file,
            "updated_at": datetime.datetime.now(datetime.UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
        self._baseline_path.write_text(json.dumps(data, indent=2) + "\n")

    def check(self, report: SuppressionReport) -> int:
        """Compare current counts against baseline. Return exit code."""
        if not self.has_baseline:
            _writeln("No baseline -- run --update to create one")
            return 0
        baseline_data = self._load_baseline()
        raw_total = baseline_data.get("total", 0)
        baseline_total = int(raw_total) if isinstance(raw_total, (int, float)) else 0
        current_total = report.total
        _writeln(f"\nBaseline total: {baseline_total}")
        _writeln(f"Current total:  {current_total}")
        if current_total > baseline_total:
            return self._report_regression(
                baseline_data, report, current_total - baseline_total
            )
        if current_total < baseline_total:
            _writeln(
                "\nPASS: suppression count decreased"
                f" by {baseline_total - current_total}"
            )
        else:
            _writeln("\nPASS: suppression count unchanged")
        return 0

    def _report_regression(
        self,
        baseline_data: dict[str, object],
        report: SuppressionReport,
        diff: int,
    ) -> int:
        """Print regression details and return exit code 1."""
        _writeln(f"\nFAIL: suppression count increased by {diff}")
        raw_by_file = baseline_data.get("by_file", {})
        baseline_by_file: dict[str, dict[str, int]] = (
            dict(raw_by_file) if isinstance(raw_by_file, dict) else {}
        )
        current_by_file = report.by_file
        _writeln("\nFiles with new or increased suppressions:")
        for fpath in sorted(set(current_by_file) | set(baseline_by_file)):
            cur = sum(current_by_file.get(fpath, {}).values())
            base = sum(baseline_by_file.get(fpath, {}).values())
            if cur > base:
                _writeln(f"  {fpath}: +{cur - base} ({base} -> {cur})")
        return 1

    def update(self, report: SuppressionReport) -> int:
        """Write current counts to baseline and append audit log."""
        self._save_baseline(report)
        self._append_audit(report)
        _writeln(f"\nBaseline updated: {self._baseline_path}")
        _writeln(f"  total: {report.total}")
        for category, count in sorted(report.by_category.items()):
            _writeln(f"  {category}: {count}")
        return 0

    def _append_audit(self, report: SuppressionReport) -> None:
        entry = {
            "ts": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "total": report.total,
            "by_category": report.by_category,
        }
        with self._audit_path.open("a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def main() -> None:
    if len(sys.argv) < 2:
        _writeln(
            f"Usage: {sys.argv[0]} <directory>"
            " [--json] [--check] [--update] [--threshold]",
        )
        sys.exit(1)

    target = Path(sys.argv[1])
    if not target.exists():
        _writeln(f"not found: {target}")
        sys.exit(1)

    scanner = Scanner(target)
    report = scanner.report
    baseline = Baseline()

    if "--check" in sys.argv:
        sys.exit(baseline.check(report))
    elif "--update" in sys.argv:
        sys.exit(baseline.update(report))
    elif "--json" in sys.argv:
        _writeln(report.to_json())
    else:
        report.print_report()
        if "--threshold" in sys.argv:
            report.print_threshold()


if __name__ == "__main__":
    main()
