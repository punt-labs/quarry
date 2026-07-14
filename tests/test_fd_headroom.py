"""Tests for the FdHeadroom file-descriptor value object."""

from __future__ import annotations

import errno
import resource
from pathlib import Path

import pytest

from quarry.fd_headroom import FdHeadroom


class TestArithmetic:
    def test_utilization_is_used_over_limit(self) -> None:
        assert FdHeadroom(open_fds=8, soft_limit=10).utilization == pytest.approx(0.8)

    def test_utilization_zero_when_limit_unbounded(self) -> None:
        # RLIM_INFINITY is the realistic "unlimited" value — a large positive
        # int, not 0 — so it must yield 0.0, never a giant denominator.
        headroom = FdHeadroom(open_fds=99, soft_limit=resource.RLIM_INFINITY)
        assert headroom.utilization == 0.0

    def test_utilization_zero_when_limit_nonpositive(self) -> None:
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


class TestSample:
    def test_sample_measures_the_running_process(self) -> None:
        headroom = FdHeadroom.sample()
        assert headroom.open_fds > 0
        assert headroom.soft_limit > 0

    def test_absent_fd_directory_raises_errno_less_oserror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Neither /proc/self/fd nor /dev/fd exists: genuine platform absence,
        # signalled by an OSError whose errno is unset (not an exhaustion code).
        monkeypatch.setattr(Path, "is_dir", lambda _self: False)
        with pytest.raises(OSError) as excinfo:
            FdHeadroom.sample()
        assert excinfo.value.errno is None

    def test_emfile_during_scan_propagates_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # At real exhaustion the iterdir scan itself raises EMFILE; sample must
        # let it propagate so callers can recognise exhaustion by errno rather
        # than mistaking it for a healthy or absent measurement.
        def _emfile(_self: Path) -> object:
            raise OSError(errno.EMFILE, "Too many open files")

        monkeypatch.setattr(Path, "is_dir", lambda _self: True)
        monkeypatch.setattr(Path, "iterdir", _emfile)
        with pytest.raises(OSError) as excinfo:
            FdHeadroom.sample()
        assert excinfo.value.errno == errno.EMFILE
