"""GpuStatus enum and its install-display classification."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, assert_never


class GpuStatus(StrEnum):
    """Outcome of a GPU runtime check, suitable for display.

    The string values are the user-facing messages and are relied on by
    ``quarry install``/``quarry doctor`` output. Classify with :attr:`outcome`
    (and the derived :attr:`is_failure`/:attr:`is_recovered`/:attr:`symbol`)
    rather than substring-matching those messages: :attr:`RESTORED`'s value
    contains the word "failed" yet is a recovered warning, not a hard failure.
    """

    NO_UV = "uv not found, skipped GPU check"
    NO_GPU = "no NVIDIA GPU"
    CUDA_PRESENT = "CUDA already available"
    INSTALLED = "onnxruntime-gpu installed"
    RESTORED = "onnxruntime-gpu install failed, CPU restored"
    RESTORE_FAILED = "onnxruntime-gpu install failed, CPU restore also failed"

    @property
    def outcome(self) -> Literal["success", "recovered", "failure"]:
        """Classify this status for install display.

        ``"failure"`` is reserved for :attr:`RESTORE_FAILED`, where the GPU
        wheel install failed *and* the CPU restore failed so the daemon cannot
        start. :attr:`RESTORED` is ``"recovered"`` — the swap fell back to a
        working CPU runtime, a warning rather than a failure. Everything else
        is ``"success"``. The match is exhaustive: a new enum member fails
        type-checking until it is classified here, so none can silently fall
        into the wrong bucket.
        """
        match self:
            case GpuStatus.RESTORE_FAILED:
                return "failure"
            case GpuStatus.RESTORED:
                return "recovered"
            case (
                GpuStatus.NO_UV
                | GpuStatus.NO_GPU
                | GpuStatus.CUDA_PRESENT
                | GpuStatus.INSTALLED
            ):
                return "success"
        assert_never(self)

    @property
    def is_failure(self) -> bool:
        """Return whether the daemon cannot start after the GPU check."""
        return self.outcome == "failure"

    @property
    def is_recovered(self) -> bool:
        """Return whether a failed GPU swap fell back to a working CPU runtime."""
        return self.outcome == "recovered"

    @property
    def symbol(self) -> str:
        """Return the install-output glyph for this status's outcome."""
        match self.outcome:
            case "failure":
                return "✗"  # cross
            case "recovered":
                return "⚠"  # warning -- daemon works
            case "success":
                return "✓"  # check
