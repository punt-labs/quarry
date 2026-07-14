"""Tests for the daemon open-fd telemetry monitor."""

from __future__ import annotations

import asyncio
import errno
import logging
import resource

import pytest

from quarry.fd_telemetry import FdTelemetry

_LOGGER_NAME = "quarry.fd_telemetry"


class TestSample:
    """Count/pct math and the INFO/WARNING gate of a single sample."""

    @staticmethod
    def _patch_fds(
        monkeypatch: pytest.MonkeyPatch, *, open_fds: int, soft_limit: int
    ) -> None:
        """Force a sample to observe *open_fds* against *soft_limit*.

        The measurement lives in ``FdHeadroom``, so the scan and rlimit seams are
        patched there. ``is_dir`` is pinned True so the count is deterministic
        regardless of which fd directory the host actually exposes.
        """
        monkeypatch.setattr("quarry.fd_headroom.Path.is_dir", lambda _self: True)
        monkeypatch.setattr(
            "quarry.fd_headroom.Path.iterdir",
            lambda _self: iter(range(open_fds)),
        )
        monkeypatch.setattr(
            "quarry.fd_headroom.resource.getrlimit",
            lambda _res: (soft_limit, soft_limit),
        )

    @staticmethod
    def _emit(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
        """Run one telemetry sample and return the emitted record."""
        with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
            FdTelemetry.sample()
        return caplog.records[-1]

    def test_computes_count_and_pct_at_info(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._patch_fds(monkeypatch, open_fds=64, soft_limit=256)
        record = self._emit(caplog)
        assert record.levelno == logging.INFO
        assert record.__dict__["open_fds"] == 64
        assert record.__dict__["fd_soft_limit"] == 256
        assert record.__dict__["fd_pct_used"] == 25.0

    def test_warns_above_threshold(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # 218 of 256 fds == 85.2%, the measured fd-exhaustion condition.
        self._patch_fds(monkeypatch, open_fds=218, soft_limit=256)
        record = self._emit(caplog)
        assert record.levelno == logging.WARNING
        assert record.__dict__["fd_pct_used"] == pytest.approx(85.2, abs=0.05)

    def test_at_threshold_stays_info(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Exactly 80% is not "> 80%", so it must not warn (boundary case).
        self._patch_fds(monkeypatch, open_fds=80, soft_limit=100)
        record = self._emit(caplog)
        assert record.levelno == logging.INFO
        assert record.__dict__["fd_pct_used"] == 80.0

    def test_unlimited_soft_limit_never_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._patch_fds(
            monkeypatch, open_fds=100_000, soft_limit=resource.RLIM_INFINITY
        )
        record = self._emit(caplog)
        assert record.levelno == logging.INFO
        assert record.__dict__["fd_pct_used"] == 0.0


class TestRun:
    """The serve-time monitor loop must outlive a failing sample."""

    async def test_sample_failure_does_not_stop_the_loop(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A raising sample (EMFILE mid-scan, or a container with no fd directory)
        # is exactly when telemetry matters most. It must log and keep ticking,
        # not die silently for the daemon's remaining life.
        calls = 0

        def _boom() -> None:
            nonlocal calls
            calls += 1
            raise OSError(errno.EMFILE, "Too many open files")

        monkeypatch.setattr(FdTelemetry, "sample", staticmethod(_boom))
        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            task = asyncio.create_task(FdTelemetry(0.001).run())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert calls > 1  # survived the first raise and kept sampling
        assert any("fd_usage sample failed" in r.message for r in caplog.records)
