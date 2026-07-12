"""Tests for SyncFinalizer's fail-open capture-shadow step and unpushed logging.

The sync tail must never let a shadow-push problem block index rebuild + GC
(fail-open), and a committed-but-not-pushed shadow must be LOGGED, not swallowed.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from quarry.shadow import ShadowSyncResult
from quarry.sync_finalize import SyncFinalizer

if TYPE_CHECKING:
    import pytest

_PUSH_REGISTERED = "quarry.shadow.CaptureSync.push_registered"


def _finalizer() -> SyncFinalizer:
    return SyncFinalizer(MagicMock(name="db"), MagicMock(name="settings"))


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
            SyncFinalizer._warn_unpushed(results)
        assert "bad" in caplog.text
        assert "ok" not in caplog.text

    def test_aborted_reason_reported(self, caplog: pytest.LogCaptureFixture) -> None:
        aborted = ShadowSyncResult.aborted("public-remote", committed=True)
        with caplog.at_level(logging.WARNING):
            SyncFinalizer._warn_unpushed({"proj": aborted})
        assert "public-remote" in caplog.text

    def test_aborted_before_commit_not_reported_as_committed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A run that aborted before commit (committed=False) made no commit, so
        # the committed-but-not-pushed phrasing would misreport it.
        aborted = ShadowSyncResult.aborted("stage-failed")
        with caplog.at_level(logging.WARNING):
            SyncFinalizer._warn_unpushed({"proj": aborted})
        assert "committed but not pushed" not in caplog.text
        assert "aborted before commit" in caplog.text
        assert "stage-failed" in caplog.text


class TestPushShadowsFailOpen:
    def test_registry_error_does_not_propagate(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fin = _finalizer()
        with (
            patch(_PUSH_REGISTERED, side_effect=sqlite3.Error("db locked")),
            caplog.at_level(logging.WARNING),
        ):
            fin._push_shadows()  # must not raise
        assert "fail-open" in caplog.text

    def test_false_push_in_background_emits_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fin = _finalizer()
        with (
            patch(_PUSH_REGISTERED, return_value={"proj": _result(pushed=False)}),
            caplog.at_level(logging.WARNING),
        ):
            fin._push_shadows()
        assert "not pushed" in caplog.text
