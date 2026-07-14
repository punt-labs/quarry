"""Tests for the fd-headroom doctor check."""

from __future__ import annotations

import errno
from typing import TYPE_CHECKING

from quarry.doctor_resources import ResourceDiagnostics
from quarry.fd_headroom import FdHeadroom

if TYPE_CHECKING:
    import pytest


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

    def test_emfile_reports_exhaustion_not_healthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The check's whole purpose is to catch exhaustion; an EMFILE raised
        # while sampling must fail the check, never masquerade as unavailable.
        def _raise(cls: type[FdHeadroom]) -> FdHeadroom:
            raise OSError(errno.EMFILE, "Too many open files")

        monkeypatch.setattr(FdHeadroom, "sample", classmethod(_raise))
        result = ResourceDiagnostics.fd_headroom()
        assert result.passed is False
        assert "exhaustion" in result.message

    def test_enfile_reports_exhaustion_not_healthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(cls: type[FdHeadroom]) -> FdHeadroom:
            raise OSError(errno.ENFILE, "Too many open files in system")

        monkeypatch.setattr(FdHeadroom, "sample", classmethod(_raise))
        result = ResourceDiagnostics.fd_headroom()
        assert result.passed is False
        assert "exhaustion" in result.message

    def test_degrades_when_measurement_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An errno-less OSError is genuine platform absence, not exhaustion:
        # the check degrades to advisory-pass so it never blocks on a container
        # that simply has no fd directory to scan.
        def _raise(cls: type[FdHeadroom]) -> FdHeadroom:
            msg = "no /proc/self/fd or /dev/fd on this platform"
            raise OSError(msg)

        monkeypatch.setattr(FdHeadroom, "sample", classmethod(_raise))
        result = ResourceDiagnostics.fd_headroom()
        assert result.passed is True
        assert "unavailable" in result.message
