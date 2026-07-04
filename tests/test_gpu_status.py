"""Tests for quarry.gpu_status — GpuStatus classification for install display."""

from __future__ import annotations

import pytest

from quarry.gpu_status import GpuStatus


class TestGpuStatusClassification:
    """Tests for GpuStatus.outcome and derived is_failure/is_recovered/symbol.

    The classification must branch on the enum member, not substring-match the
    message: RESTORED's value contains the word "failed" yet is a recovered
    warning, not a hard failure.
    """

    def test_restore_failed_is_failure(self) -> None:
        assert GpuStatus.RESTORE_FAILED.outcome == "failure"
        assert GpuStatus.RESTORE_FAILED.is_failure is True
        assert GpuStatus.RESTORE_FAILED.is_recovered is False
        assert GpuStatus.RESTORE_FAILED.symbol == "✗"

    def test_restored_is_recovered_not_failure(self) -> None:
        """RESTORED contains 'failed' in its message but is a recovered warning."""
        assert "failed" in GpuStatus.RESTORED  # the substring that caused the bug
        assert GpuStatus.RESTORED.outcome == "recovered"
        assert GpuStatus.RESTORED.is_failure is False
        assert GpuStatus.RESTORED.is_recovered is True
        assert GpuStatus.RESTORED.symbol == "⚠"

    @pytest.mark.parametrize(
        "status",
        [
            GpuStatus.NO_UV,
            GpuStatus.NO_GPU,
            GpuStatus.CUDA_PRESENT,
            GpuStatus.INSTALLED,
        ],
    )
    def test_happy_states_are_success(self, status: GpuStatus) -> None:
        assert status.outcome == "success"
        assert status.is_failure is False
        assert status.is_recovered is False
        assert status.symbol == "✓"

    def test_every_member_is_classified(self) -> None:
        """Every enum member resolves to a known outcome — no member falls through."""
        for status in GpuStatus:
            assert status.outcome in {"success", "recovered", "failure"}
