"""Tests for SyncFinalizer's fail-open capture-shadow step and unpushed logging.

The sync tail must never let a shadow-push problem block index rebuild + GC
(fail-open), and a committed-but-not-pushed shadow must be LOGGED, not swallowed.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from quarry.db.optimizer import OptimizeOutcome
from quarry.finalize_ledger import FinalizeLedgers
from quarry.finalize_steps import OptimizeStep, ShadowPushStep
from quarry.shadow import ShadowSyncResult
from quarry.sync_finalize import SyncFinalizer

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_PUSH_REGISTERED = "quarry.shadow.CaptureSync.push_registered"


def _result(*, pushed: bool) -> ShadowSyncResult:
    return ShadowSyncResult(
        pushed=pushed,
        committed=True,
        rescrubbed=0,
        aborted_reason="",
        race_failures=(),
    )


class TestWarnUnpushed:
    def test_only_unpushed_is_warned(self, caplog: pytest.LogCaptureFixture) -> None:
        results = {"ok": _result(pushed=True), "bad": _result(pushed=False)}
        with caplog.at_level(logging.WARNING):
            ShadowPushStep._warn_unpushed(results)
        assert "bad" in caplog.text
        assert "ok" not in caplog.text

    def test_aborted_reason_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        aborted = ShadowSyncResult.aborted("public-remote", committed=True)
        with caplog.at_level(logging.WARNING):
            ShadowPushStep._warn_unpushed({"proj": aborted})
        assert "public-remote" in caplog.text

    def test_aborted_before_commit_not_reported_as_committed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A run that aborted before commit (committed=False) made no commit, so
        # the committed-but-not-pushed phrasing would misreport it.
        aborted = ShadowSyncResult.aborted("stage-failed")
        with caplog.at_level(logging.WARNING):
            ShadowPushStep._warn_unpushed({"proj": aborted})
        assert "committed but not pushed" not in caplog.text
        assert "aborted before commit" in caplog.text
        assert "stage-failed" in caplog.text

    def test_not_committed_not_aborted_is_not_reported_as_abort(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A run that neither committed nor aborted (nothing to commit or a
        # deferred offline push) must not be phrased as an abort.
        deferred = ShadowSyncResult(
            pushed=False,
            committed=False,
            rescrubbed=0,
            aborted_reason="",
            race_failures=(),
        )
        with caplog.at_level(logging.WARNING):
            ShadowPushStep._warn_unpushed({"proj": deferred})
        assert "aborted before commit" not in caplog.text
        assert "not pushed" in caplog.text


class TestPushShadowsFailOpen:
    def test_registry_error_does_not_propagate(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch(_PUSH_REGISTERED, side_effect=sqlite3.Error("db locked")),
            caplog.at_level(logging.WARNING),
        ):
            ShadowPushStep(MagicMock(name="settings")).run()  # must not raise
        assert "fail-open" in caplog.text

    def test_false_push_in_background_emits_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch(_PUSH_REGISTERED, return_value={"proj": _result(pushed=False)}),
            caplog.at_level(logging.WARNING),
        ):
            ShadowPushStep(MagicMock(name="settings")).run()
        assert "not pushed" in caplog.text


def _finalizer_at(path: Path, root: Path | None = None) -> _Stepper:
    settings = MagicMock(name="settings")
    settings.lancedb_path = path
    settings.quarry_root = root or path.parent.parent
    return _Stepper(MagicMock(name="db"), settings)


class _Stepper:
    def __init__(self, db: MagicMock, settings: MagicMock) -> None:
        self._fin = SyncFinalizer(db, settings)
        self._step = OptimizeStep(db, settings)

    def run(self) -> None:
        self._fin.run()

    def run_optimize_only(self) -> None:
        self._step.run()


class TestOptimizeFailureIsolation:
    def test_optimize_failure_does_not_skip_shadow_push_or_gc(
        self, tmp_path: Path
    ) -> None:
        fin = _finalizer_at(tmp_path / "lancedb")
        with (
            patch("quarry.finalize_steps.TableOptimizer") as optimizer,
            patch("quarry.sync_finalize.ShadowPushStep") as push,
            patch("quarry.table_upkeep.PostSyncGc") as collect,
        ):
            optimizer.return_value.optimize.side_effect = RuntimeError(
                "Offset overflow error"
            )
            fin.run()
        push.assert_called_once()
        collect.return_value.run.assert_called_once()

    def test_failure_is_recorded_by_exception_type(self, tmp_path: Path) -> None:
        path = tmp_path / "lancedb"
        with (
            patch("quarry.finalize_steps.TableOptimizer") as optimizer,
            patch("quarry.sync_finalize.ShadowPushStep"),
            patch("quarry.table_upkeep.PostSyncGc"),
        ):
            optimizer.return_value.optimize.side_effect = RuntimeError(
                "Offset overflow error"
            )
            _finalizer_at(path).run()
        (entry,) = (
            h for h in FinalizeLedgers.snapshot() if h.database == path.parent.name
        )
        assert entry.consecutive_failures == 1
        assert entry.last_error == "RuntimeError"

    def test_backed_off_database_skips_optimize_but_still_runs_tail(
        self, tmp_path: Path
    ) -> None:
        fin = _finalizer_at(tmp_path / "lancedb")
        with (
            patch("quarry.finalize_steps.TableOptimizer") as optimizer,
            patch("quarry.sync_finalize.ShadowPushStep") as push,
            patch("quarry.table_upkeep.PostSyncGc") as collect,
        ):
            optimizer.return_value.optimize.side_effect = RuntimeError("boom")
            fin.run()
            fin.run()
        assert optimizer.return_value.optimize.call_count == 1
        assert push.call_count == 2
        assert collect.return_value.run.call_count == 2


class TestOptimizeOutcomeIsInspected:
    def test_threshold_skip_is_degraded_not_success(self, tmp_path: Path) -> None:
        from quarry.db.optimizer import OptimizeOutcome

        path = tmp_path / "skipdb" / "lancedb"
        ledger = FinalizeLedgers.for_database(path, tmp_path)
        ledger.clear()
        skip = OptimizeOutcome(optimized=False, fragments=20_000, reason="too many")
        with patch("quarry.finalize_steps.TableOptimizer") as optimizer:
            optimizer.return_value.optimize.return_value = skip
            _finalizer_at(path).run_optimize_only()
        (entry,) = (h for h in FinalizeLedgers.snapshot() if h.database == "skipdb")
        assert entry.consecutive_failures == 1
        assert "--force" in entry.last_error

    def test_no_table_leaves_failure_state_untouched(self, tmp_path: Path) -> None:
        from quarry.db.optimizer import OptimizeOutcome

        path = tmp_path / "notable" / "lancedb"
        FinalizeLedgers.for_database(path, tmp_path).clear()
        none = OptimizeOutcome(optimized=False, fragments=0, reason="no table")
        with patch("quarry.finalize_steps.TableOptimizer") as optimizer:
            optimizer.return_value.optimize.return_value = none
            _finalizer_at(path).run_optimize_only()
        (entry,) = (h for h in FinalizeLedgers.snapshot() if h.database == "notable")
        assert entry.consecutive_failures == 0


class TestHealthIsCoarse:
    def test_wire_carries_name_and_exception_type_only(self, tmp_path: Path) -> None:
        path = tmp_path / "secretuser" / "mydb" / "lancedb"
        root = tmp_path / "secretuser"
        with (
            patch("quarry.finalize_steps.TableOptimizer") as optimizer,
            patch("quarry.sync_finalize.ShadowPushStep"),
            patch("quarry.table_upkeep.PostSyncGc"),
        ):
            optimizer.return_value.optimize.side_effect = RuntimeError(
                f"cannot read {path}/data/x.lance"
            )
            _finalizer_at(path, root).run()
        wire = [
            h.model_dump_json()
            for h in FinalizeLedgers.snapshot()
            if h.database == "mydb"
        ]
        assert wire
        assert "secretuser" not in wire[0]
        assert "x.lance" not in wire[0]
        assert "RuntimeError" in wire[0]


class TestOrphanPageSweepStep:
    def test_finalize_sweeps_orphan_pages_after_optimize(self, tmp_path: Path) -> None:
        fin = _finalizer_at(tmp_path / "lancedb")
        calls: list[str] = []

        def optimize(**_: object) -> OptimizeOutcome:
            calls.append("optimize")
            return OptimizeOutcome(optimized=True, fragments=1)

        def sweep_run() -> int:
            calls.append("sweep")
            return 0

        with (
            patch("quarry.finalize_steps.TableOptimizer") as optimizer,
            patch("quarry.table_upkeep.OrphanPageSweep") as sweep,
            patch("quarry.sync_finalize.ShadowPushStep"),
            patch("quarry.table_upkeep.PostSyncGc"),
        ):
            optimizer.return_value.optimize.side_effect = optimize
            sweep.return_value.run.side_effect = sweep_run
            fin.run()
        assert calls == ["optimize", "sweep"]

    def test_a_failing_sweep_does_not_skip_shadow_push_or_gc(
        self, tmp_path: Path
    ) -> None:
        fin = _finalizer_at(tmp_path / "lancedb")
        with (
            patch("quarry.finalize_steps.TableOptimizer"),
            patch("quarry.table_upkeep.OrphanPageSweep") as sweep,
            patch("quarry.sync_finalize.ShadowPushStep") as push,
            patch("quarry.table_upkeep.PostSyncGc") as collect,
        ):
            sweep.return_value.run.side_effect = OSError("disk")
            fin.run()
        push.assert_called_once()
        collect.return_value.run.assert_called_once()
