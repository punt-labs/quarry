"""Tests for the fd-headroom doctor check."""

from __future__ import annotations

import pytest

from quarry.doctor_resources import FdHeadroom, ResourceDiagnostics


class TestFdHeadroom:
    def test_utilization_is_used_over_limit(self) -> None:
        assert FdHeadroom(open_fds=8, soft_limit=10).utilization == pytest.approx(0.8)

    def test_utilization_zero_when_limit_unbounded(self) -> None:
        assert FdHeadroom(open_fds=99, soft_limit=0).utilization == 0.0

    def test_is_low_above_threshold(self) -> None:
        assert FdHeadroom(open_fds=9, soft_limit=10).is_low is True

    def test_is_low_false_at_exactly_threshold(self) -> None:
        # 80% is the warning boundary; the check fires strictly above it.
        assert FdHeadroom(open_fds=8, soft_limit=10).is_low is False

    def test_describe_reports_used_limit_and_percent(self) -> None:
        assert (
            FdHeadroom(open_fds=200, soft_limit=256).describe() == "200/256 fds (78%)"
        )

    def test_sample_measures_the_running_process(self) -> None:
        headroom = FdHeadroom.sample()
        assert headroom.open_fds > 0
        assert headroom.soft_limit > 0


class TestFdHeadroomCheck:
    def test_warns_when_over_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            FdHeadroom, "sample", classmethod(lambda cls: FdHeadroom(250, 256))
        )
        result = ResourceDiagnostics.fd_headroom()
        assert result.passed is False
        assert "over 80%" in result.message
        assert result.required is False

    def test_passes_when_healthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            FdHeadroom, "sample", classmethod(lambda cls: FdHeadroom(30, 256))
        )
        result = ResourceDiagnostics.fd_headroom()
        assert result.passed is True
        assert "30/256" in result.message

    def test_degrades_when_measurement_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(cls: type[FdHeadroom]) -> FdHeadroom:
            msg = "no /proc/self/fd or /dev/fd on this platform"
            raise OSError(msg)

        monkeypatch.setattr(FdHeadroom, "sample", classmethod(_raise))
        result = ResourceDiagnostics.fd_headroom()
        assert result.passed is True
        assert "unavailable" in result.message
