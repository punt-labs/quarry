"""Tests for SyncFinalizer's fail-open capture-shadow step and unpushed logging.

The sync tail must never let a shadow-push problem block index rebuild + GC
(fail-open), and a committed-but-not-pushed shadow must be LOGGED, not swallowed.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from quarry.finalize_ledger import FinalizeLedgers
from quarry.finalize_steps import ShadowPushStep
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


def _finalizer_at(path: Path) -> SyncFinalizer:
    settings = MagicMock(name="settings")
    settings.lancedb_path = path
    return SyncFinalizer(MagicMock(name="db"), settings)


class TestOptimizeFailureIsolation:
    def test_optimize_failure_does_not_skip_shadow_push_or_gc(
        self, tmp_path: Path
    ) -> None:
        fin = _finalizer_at(tmp_path / "lancedb")
        with (
            patch("quarry.finalize_steps.TableOptimizer") as optimizer,
            patch("quarry.sync_finalize.ShadowPushStep") as push,
            patch.object(SyncFinalizer, "_gc") as collect,
        ):
            optimizer.return_value.optimize.side_effect = RuntimeError(
                "Offset overflow error"
            )
            fin.run()
        push.assert_called_once()
        collect.assert_called_once()

    def test_failure_is_recorded_with_its_reason(self, tmp_path: Path) -> None:
        path = tmp_path / "lancedb"
        with (
            patch("quarry.finalize_steps.TableOptimizer") as optimizer,
            patch("quarry.sync_finalize.ShadowPushStep"),
            patch.object(SyncFinalizer, "_gc"),
        ):
            optimizer.return_value.optimize.side_effect = RuntimeError(
                "Offset overflow error"
            )
            _finalizer_at(path).run()
        (entry,) = (h for h in FinalizeLedgers.snapshot() if h.database == str(path))
        assert entry.consecutive_failures == 1
        assert "Offset overflow error" in entry.last_error

    def test_backed_off_database_skips_optimize_but_still_runs_tail(
        self, tmp_path: Path
    ) -> None:
        fin = _finalizer_at(tmp_path / "lancedb")
        with (
            patch("quarry.finalize_steps.TableOptimizer") as optimizer,
            patch("quarry.sync_finalize.ShadowPushStep") as push,
            patch.object(SyncFinalizer, "_gc") as collect,
        ):
            optimizer.return_value.optimize.side_effect = RuntimeError("boom")
            fin.run()
            fin.run()
        assert optimizer.return_value.optimize.call_count == 1
        assert push.call_count == 2
        assert collect.call_count == 2
