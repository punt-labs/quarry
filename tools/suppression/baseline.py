"""Suppression baseline: persistence, ratchet check, and audit logging."""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import ClassVar, Self

from .audit import SuppressionAudit
from .gitio import GitRepo
from .outcome import Outcome
from .report import SuppressionReport


class SuppressionBaselineError(Exception):
    """The in-tree ``.suppression-baseline.json`` could not be parsed.

    Raised instead of letting ``json.JSONDecodeError`` escape, so a corrupt or
    hand-broken baseline becomes a controlled non-zero outcome (the CLI catches
    it) rather than a traceback out of the gate.
    """


class SuppressionBaseline:
    """Persist suppression counts and refuse any increase against the baseline.

    The comparison baseline is read from the base commit
    (``git show <base>:.suppression-baseline.json``), not the worktree file, so a
    PR cannot launder a rising count by hand-editing the in-tree baseline. The
    in-tree file is parsed at construction only to validate it (a committed
    corrupt or non-dict baseline fails the gate), matching ``CouplingBaseline``.
    """

    _baseline_path: Path
    _git: GitRepo
    _audit: SuppressionAudit
    _entries: dict[str, object]

    BASELINE_FILE: ClassVar[str] = ".suppression-baseline.json"

    def __new__(cls, root: Path | None = None) -> Self:
        self = super().__new__(cls)
        base = root if root is not None else Path.cwd()
        self._baseline_path = base / cls.BASELINE_FILE
        self._git = GitRepo(base)
        self._audit = SuppressionAudit(base)
        self._entries = self._load()  # eager: a corrupt in-tree file fails here
        return self

    @property
    def has_baseline(self) -> bool:
        """Return whether a baseline file exists on disk."""
        return self._baseline_path.exists()

    def check(
        self, report: SuppressionReport, *, base_ref: str | None, require_base: bool
    ) -> Outcome:
        """Compare current counts against the base-commit suppression baseline."""
        base = self._git.resolve_base(base_ref)
        if base is None:
            return self._no_base(require_base=require_base)
        base_data = self._git.show_baseline(base)
        if base_data is None:
            return self._absent_base()
        waivable = self._audit.relaxations_since(self._git.show_audit(base))
        return self._compare(report, base_data, waivable)

    def _no_base(self, *, require_base: bool) -> Outcome:
        """Decide the verdict when no comparison base can be resolved.

        Matches the OO and coupling ratchets' ``_no_base`` exactly: fail closed
        under ``--require-base``; a genuine first-adoption (no in-tree baseline)
        passes so the first baseline can be created; but an in-tree baseline
        present with an unresolvable base means a stale or unfetched
        ``origin/main`` -- fail loud rather than trust a hand-editable file.
        """
        if require_base:
            return Outcome.failed(
                "FAIL: base ref unresolvable and --require-base is set"
            )
        if not self.has_baseline:
            return Outcome.passed(
                "No base and no in-tree baseline -- first-adoption bootstrap pass"
            )
        return Outcome.failed(
            "FAIL: cannot resolve merge-base (origin/main unfetched or stale) "
            "with an in-tree baseline present; fetch origin/main or pass --base-ref"
        )

    def _absent_base(self) -> Outcome:
        """Decide the verdict when the base commit carries no baseline blob.

        Matches the OO and coupling ratchets' ``_absent_base_baseline`` exactly
        (no ``require_base`` param): fail closed unconditionally when the
        ``origin/main`` tip is unresolvable with an in-tree baseline present, or
        when the tip carries a baseline (the branch forked before adoption).
        """
        tip = self._git.resolve_ref("origin/main")
        if tip is None:
            if self.has_baseline:
                return Outcome.failed(
                    "FAIL: base has no baseline and origin/main is unresolvable "
                    "with an in-tree baseline present; fetch origin/main"
                )
            return Outcome.passed(
                "No base baseline and no origin/main -- first-adoption bootstrap pass"
            )
        if self._git.show_baseline(tip) is not None:
            return Outcome.failed(
                "FAIL: base commit predates baseline adoption; rebase onto current main"
            )
        return Outcome.passed(
            "No baseline at base or origin/main tip -- first-adoption bootstrap pass"
        )

    def _compare(
        self,
        report: SuppressionReport,
        data: dict[str, object],
        waivable: frozenset[str],
    ) -> Outcome:
        baseline_total = self._as_int(data.get("total", 0))
        current_total = report.total
        baseline_by_file = self._baseline_by_file(data)
        current_by_file = report.by_file
        forgiven = self._forgiven_increase(baseline_by_file, current_by_file, waivable)
        adjusted_total = baseline_total + forgiven
        head = [
            f"\nBaseline total: {baseline_total}",
            f"Current total:  {current_total}",
        ]
        if forgiven:
            head.append(f"Relaxed by audited --relax: +{forgiven}")
        if current_total > adjusted_total:
            lines = self._regression(
                baseline_by_file, current_by_file, current_total, adjusted_total
            )
            return Outcome(1, tuple(head + lines))
        if current_total < baseline_total:
            drop = baseline_total - current_total
            return Outcome.passed(
                *head, f"\nPASS: suppression count decreased by {drop}"
            )
        return Outcome.passed(*head, "\nPASS: suppression count unchanged")

    def _forgiven_increase(
        self,
        baseline_by_file: dict[str, dict[str, int]],
        current_by_file: dict[str, dict[str, int]],
        waivable: frozenset[str],
    ) -> int:
        """Return the total increase legitimately waived by an audited ``--relax``.

        A file's increase is forgiven only when it was named by a relaxation
        recorded since the comparison base (``waivable``) AND the in-tree
        baseline is locked to its current count -- an un-committed local edit
        can never forge a waiver, only a committed ``--relax`` can.
        """
        intree_by_file = self._baseline_by_file(self._entries)
        forgiven = 0
        for path in waivable:
            base_count = sum(baseline_by_file.get(path, {}).values())
            cur_count = sum(current_by_file.get(path, {}).values())
            intree_count = sum(intree_by_file.get(path, {}).values())
            if cur_count > base_count and intree_count == cur_count:
                forgiven += cur_count - base_count
        return forgiven

    def update(self, report: SuppressionReport, *, allow_ci_write: bool) -> Outcome:
        """Write current counts to the baseline, never loosening.

        Refuses any net increase over the in-tree baseline total: an update that
        would raise the count writes nothing and fails, exactly like the OO and
        coupling writers refuse a per-metric regression. A decrease or unchanged
        total writes normally; genuine first-adoption (no in-tree baseline)
        bootstraps.
        """
        blocked = self._guard(allow_ci_write=allow_ci_write)
        if blocked is not None:
            return blocked
        refused = self._refuse_increase(report)
        if refused is not None:
            return refused
        self._save(report)
        self._audit.append(
            total=report.total,
            by_category=report.by_category,
            verdict="update",
            deltas={},
            commit=self._git.short_head(),
        )
        lines = [
            f"\nBaseline updated: {self._baseline_path}",
            f"  total: {report.total}",
        ]
        lines.extend(
            f"  {category}: {count}"
            for category, count in sorted(report.by_category.items())
        )
        return Outcome.passed(*lines)

    def relax(
        self,
        report: SuppressionReport,
        file: str,
        *,
        justify: str,
        allow_ci_write: bool,
        source: str | None,
    ) -> Outcome:
        """Write ``file``'s current suppression counts even if higher, with reason.

        The single sanctioned, audited increase -- mirrors
        ``CouplingWriter.relax``. Only the named file's counts move; every other
        file's baseline entry, and the whole-tree total/by_category, are adjusted
        by exactly that file's delta so an unrelated in-flight change elsewhere in
        the tree cannot ride along on the same relaxation.
        """
        blocked = self._guard(allow_ci_write=allow_ci_write)
        if blocked is not None:
            return blocked
        if not justify.strip():
            return Outcome.failed("FAIL: --relax requires a non-empty --justify")
        current_counts = report.by_file.get(file, {})
        current_total = sum(current_counts.values())
        intree_by_file = self._baseline_by_file(self._entries)
        base_counts = intree_by_file.get(file, {})
        base_total = sum(base_counts.values())
        if current_total <= base_total:
            return Outcome.failed(
                f"FAIL: {file} has {current_total} suppression(s), not more than "
                f"its baseline {base_total}; that is a paydown -- use --update"
            )
        deltas = {
            category: [base_counts.get(category, 0), current_counts.get(category, 0)]
            for category in set(base_counts) | set(current_counts)
            if base_counts.get(category, 0) != current_counts.get(category, 0)
        }
        new_by_category = self._category_totals(self._entries)
        for category, (old, new) in deltas.items():
            new_by_category[category] = new_by_category.get(category, 0) - old + new
        new_by_file = dict(intree_by_file)
        new_by_file[file] = current_counts
        old_total = self._as_int(self._entries.get("total", 0))
        new_total = old_total - base_total + current_total
        self._write(new_total, new_by_category, new_by_file)
        self._audit.append(
            total=new_total,
            by_category=new_by_category,
            verdict="relaxed",
            deltas={file: deltas},
            commit=self._git.short_head(),
            source=source,
            reason=justify,
        )
        return Outcome.passed(
            f"\nRelaxed {file} (reason: {justify})",
            f"  baseline: {self._baseline_path}",
            f"  {file}: {base_total} -> {current_total}",
        )

    @staticmethod
    def _guard(*, allow_ci_write: bool) -> Outcome | None:
        if os.environ.get("GITHUB_ACTIONS") == "true" and not allow_ci_write:
            return Outcome.failed(
                "FAIL: refusing to write suppression baseline under GITHUB_ACTIONS "
                "without --allow-ci-write"
            )
        return None

    def _refuse_increase(self, report: SuppressionReport) -> Outcome | None:
        """Return a failure ``Outcome`` if the update would raise the total.

        Genuine first-adoption (no in-tree baseline) bootstraps, mirroring the
        coupling/OO writers, which write new entries with no base to regress
        against. An existing baseline is never loosened: a rise writes nothing.
        """
        if not self.has_baseline:
            return None
        baseline_total = self._as_int(self._entries.get("total", 0))
        if report.total > baseline_total:
            rise = report.total - baseline_total
            return Outcome.failed(
                f"\nBaseline total: {baseline_total}",
                f"Current total:  {report.total}",
                f"\nFAIL: refusing to raise the suppression baseline by {rise} "
                f"({baseline_total} -> {report.total}); update never loosens",
            )
        return None

    @staticmethod
    def _regression(
        baseline_by_file: dict[str, dict[str, int]],
        current_by_file: dict[str, dict[str, int]],
        current_total: int,
        adjusted_total: int,
    ) -> list[str]:
        # current_total is report.total (files + per_file_ignores), passed in
        # rather than resummed from current_by_file, which excludes the
        # config-level per_file_ignores category and would understate the diff.
        diff = current_total - adjusted_total
        lines = [
            f"\nFAIL: suppression count increased by {diff}",
            "\nFiles with new or increased suppressions:",
        ]
        sum_of_file_increases = 0
        for fpath in sorted(set(current_by_file) | set(baseline_by_file)):
            cur = sum(current_by_file.get(fpath, {}).values())
            base = sum(baseline_by_file.get(fpath, {}).values())
            if cur > base:
                rise = cur - base
                sum_of_file_increases += rise
                lines.append(f"  {fpath}: +{rise} ({base} -> {cur})")
        if sum_of_file_increases != diff:
            # Gap sources: paydowns on files not in the regression set, shifts in
            # config-level per_file_ignores, and audited --relax waivers. Stay
            # neutral about cause; just state the arithmetic.
            lines.append(
                f"\nNet after per-file offsets and any --relax waivers: +{diff} "
                f"(sum of per-file increases above: +{sum_of_file_increases})"
            )
        return lines

    @classmethod
    def _category_totals(cls, data: dict[str, object]) -> dict[str, int]:
        raw = data.get("by_category", {})
        if not isinstance(raw, dict):
            return {}
        return {str(k): cls._as_int(v) for k, v in raw.items()}

    @classmethod
    def _baseline_by_file(cls, data: dict[str, object]) -> dict[str, dict[str, int]]:
        raw = data.get("by_file", {})
        if not isinstance(raw, dict):
            return {}
        # Coerce each per-file entry to a dict of int counts. A non-dict entry is
        # dropped and a non-numeric count becomes 0 -- both fail-closed: the
        # baseline counts smaller, so a current count registers as an increase
        # rather than crashing ``sum(...values())`` in _regression.
        result: dict[str, dict[str, int]] = {}
        for path, counts in raw.items():
            if isinstance(counts, dict):
                result[path] = {k: cls._as_int(v) for k, v in counts.items()}
        return result

    @staticmethod
    def _as_int(raw: object) -> int:
        # bool is a subclass of int; a bool count (`true`) is invalid data and
        # must be 0, not coerced to 1 -- coercing to 1 would INFLATE the baseline
        # (fail-open). Reject it before the numeric handling.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 0
        # json.loads parses NaN/Infinity, and int(nan)/int(inf) raise
        # ValueError/OverflowError. Coerce those to 0, consistent with the
        # non-numeric -> 0 contract, so a corrupt baseline never throws.
        try:
            return int(raw)
        except (ValueError, OverflowError):
            return 0

    def _load(self) -> dict[str, object]:
        if not self._baseline_path.exists():
            return {}
        try:
            raw = json.loads(self._baseline_path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            msg = f"unreadable suppression baseline file {self._baseline_path}: {exc}"
            raise SuppressionBaselineError(msg) from exc
        if not isinstance(raw, dict):
            msg = f"non-dict suppression baseline file {self._baseline_path}"
            raise SuppressionBaselineError(msg)
        return raw

    def _save(self, report: SuppressionReport) -> None:
        self._write(report.total, dict(report.by_category), dict(report.by_file))

    def _write(
        self,
        total: int,
        by_category: dict[str, int],
        by_file: dict[str, dict[str, int]],
    ) -> None:
        """Write the baseline to disk and refresh the in-memory view."""
        data = {
            "total": total,
            "by_category": by_category,
            "by_file": by_file,
            "updated_at": self._now(),
        }
        self._baseline_path.write_text(json.dumps(data, indent=2) + "\n")
        self._entries = data

    @staticmethod
    def _now() -> str:
        return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
