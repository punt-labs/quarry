"""Command-line entry point: parse arguments and dispatch to the ratchet."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .audit import SuppressionAuditError
from .baseline import SuppressionBaseline, SuppressionBaselineError
from .gitio import GitError, GitRepo
from .outcome import Outcome
from .pyproject import PyprojectError
from .report import SuppressionReport
from .scanner import Scanner


@dataclass(frozen=True, slots=True)
class Options:
    """Parsed command-line options for one suppression ratchet invocation."""

    src: Path
    check: bool
    update: bool
    json: bool
    threshold: bool
    relax: str | None
    justify: str
    base_ref: str | None
    require_base: bool
    allow_ci_write: bool
    source: str | None

    @classmethod
    def parse(cls, argv: list[str] | None = None) -> Self:
        """Build options from ``argv`` (defaults to ``sys.argv``)."""
        ns = cls._build_parser().parse_args(argv)
        return cls(
            src=Path(ns.src),
            check=bool(ns.check),
            update=bool(ns.update),
            json=bool(ns.json),
            threshold=bool(ns.threshold),
            relax=ns.relax,
            justify=ns.justify or "",
            base_ref=ns.base_ref,
            require_base=bool(ns.require_base),
            allow_ci_write=bool(ns.allow_ci_write),
            source=ns.source,
        )

    @staticmethod
    def _build_parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="suppression_ratchet",
            description="count lint/type suppressions and enforce a ratchet",
        )
        parser.add_argument("src", help="file or directory to scan")
        action = parser.add_mutually_exclusive_group()
        action.add_argument("--check", action="store_true", help="ratchet check")
        action.add_argument("--update", action="store_true", help="update baseline")
        action.add_argument("--threshold", action="store_true", help="per-file table")
        action.add_argument("--json", action="store_true", help="emit JSON counts")
        action.add_argument("--relax", metavar="FILE", help="relax one file's baseline")
        parser.add_argument("--justify", default="", help="justification for --relax")
        parser.add_argument("--base-ref", metavar="REF", help="comparison base commit")
        parser.add_argument(
            "--require-base", action="store_true", help="fail if base unresolvable"
        )
        parser.add_argument(
            "--allow-ci-write", action="store_true", help="permit writes under CI"
        )
        parser.add_argument("--source", metavar="REF", help="audit source (PR/bead)")
        return parser


class Cli:
    """Scan the target and route the request to the ratchet or a view."""

    _opts: Options
    _git: GitRepo
    _root: Path

    def __new__(cls, options: Options) -> Self:
        self = super().__new__(cls)
        self._opts = options
        self._git = GitRepo()
        self._root = self._git.root or Path.cwd()
        return self

    def run(self) -> int:
        """Execute the requested operation and return its exit code."""
        if not self._opts.src.exists():
            return self._emit(Outcome.failed(f"not found: {self._opts.src}"))
        try:
            # Anchor the scan's project root and the baseline path to the repo
            # root (via GitRepo), not cwd, so running from a subdirectory still
            # reads the right pyproject.toml and .suppression-baseline.json.
            # Scan and construct inside the try: an unreadable .py file (OSError),
            # a corrupt in-tree baseline (eager load), or a corrupt audit log
            # (read on --check/--relax) must surface as a clean non-zero, not a
            # traceback.
            report = Scanner(self._opts.src, self._root).report
            outcome = self._dispatch(SuppressionBaseline(self._root), report)
        except (
            GitError,
            SuppressionBaselineError,
            SuppressionAuditError,
            PyprojectError,
            OSError,
            UnicodeDecodeError,
        ) as exc:
            outcome = Outcome.failed(f"FAIL: {exc}")
        return self._emit(outcome)

    def _dispatch(
        self, baseline: SuppressionBaseline, report: SuppressionReport
    ) -> Outcome:
        opts = self._opts
        if opts.check:
            return baseline.check(
                report, base_ref=opts.base_ref, require_base=opts.require_base
            )
        if opts.update:
            return baseline.update(report, allow_ci_write=opts.allow_ci_write)
        if opts.relax is not None:
            return baseline.relax(
                report,
                opts.relax,
                justify=opts.justify,
                allow_ci_write=opts.allow_ci_write,
                source=opts.source,
            )
        if opts.json:
            return Outcome.passed(report.to_json())
        lines = list(report.render())
        if opts.threshold:
            lines.extend(report.render_threshold())
        return Outcome.passed(*lines)

    @staticmethod
    def _emit(outcome: Outcome) -> int:
        for line in outcome.lines:
            print(line)
        return outcome.exit_code


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the suppression ratchet CLI, returning a code."""
    return Cli(Options.parse(argv)).run()
