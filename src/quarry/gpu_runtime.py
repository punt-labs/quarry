# ruff: noqa: S603 — subprocess calls invoke trusted system binaries (uv, nvidia-smi)
"""NVIDIA GPU runtime detection and onnxruntime package swapping.

``quarry install`` and ``quarry doctor`` call :meth:`GpuRuntime.ensure` to
swap the CPU-only ``onnxruntime`` wheel for ``onnxruntime-gpu`` when an NVIDIA
GPU is present.  The swap is best-effort: on any failure the CPU runtime is
restored so the daemon still starts.  Safe to call on any platform — it returns
early when ``uv`` or ``nvidia-smi`` is absent (macOS, CPU-only Linux).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from typing import Self

from quarry.gpu_status import GpuStatus

logger = logging.getLogger(__name__)

_ORT_GPU_SPEC = "onnxruntime-gpu>=1.18.0"
_ORT_CPU_SPEC = "onnxruntime>=1.18.0"


class GpuRuntime:
    """Swap onnxruntime for onnxruntime-gpu when an NVIDIA GPU is present."""

    _uv_path: str
    _python: str

    def __new__(cls, uv_path: str) -> Self:
        self = super().__new__(cls)
        self._uv_path = uv_path
        self._python = sys.executable
        return self

    @classmethod
    def ensure(cls) -> GpuStatus:
        """Detect the GPU and swap the onnxruntime package, returning a status.

        Returns early with :attr:`GpuStatus.NO_UV` when ``uv`` is not on PATH,
        since the swap requires it.  Otherwise delegates to the swap workflow.
        """
        uv_path = shutil.which("uv")
        if uv_path is None:
            logger.info("uv not on PATH — skipping GPU runtime check")
            return GpuStatus.NO_UV
        return cls(uv_path)._resolve()

    def _resolve(self) -> GpuStatus:
        """Run the detection/swap workflow once ``uv`` is known to be present."""
        if not self._gpu_present():
            return GpuStatus.NO_GPU
        if self._cuda_available():
            logger.info("CUDAExecutionProvider already available")
            return GpuStatus.CUDA_PRESENT
        return self._swap()

    @staticmethod
    def _gpu_present() -> bool:
        """Return ``True`` when ``nvidia-smi`` exists and reports a usable GPU."""
        nvidia_smi = shutil.which("nvidia-smi")
        if nvidia_smi is None:
            logger.info("nvidia-smi not found — no NVIDIA GPU")
            return False
        result = subprocess.run(
            [nvidia_smi],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            logger.info(
                "nvidia-smi failed (rc=%d) — no usable NVIDIA GPU", result.returncode
            )
            return False
        return True

    def _cuda_available(self) -> bool:
        """Return ``True`` when the current interpreter already exposes CUDA.

        Uses a subprocess to avoid stale native shared libraries (``.so``) that
        persist in this process after a previous onnxruntime import.
        """
        provider_check = subprocess.run(
            [
                self._python,
                "-c",
                "import onnxruntime; "
                "print(','.join(onnxruntime.get_available_providers()))",
            ],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        return (
            provider_check.returncode == 0
            and "CUDAExecutionProvider" in provider_check.stdout
        )

    def _swap(self) -> GpuStatus:
        """Replace CPU onnxruntime with onnxruntime-gpu, restoring CPU on failure."""
        logger.info(
            "Swapping onnxruntime for onnxruntime-gpu (python=%s)", self._python
        )
        # Uninstall CPU onnxruntime (suppress errors — may not be installed).
        self._pip("uninstall", "onnxruntime")
        gpu_install = self._pip("install", _ORT_GPU_SPEC)
        if gpu_install.returncode == 0:
            logger.info("onnxruntime-gpu installed successfully")
            self._clear_module_cache()
            return GpuStatus.INSTALLED
        logger.warning(
            "onnxruntime-gpu install failed (rc=%d), restoring CPU runtime",
            gpu_install.returncode,
        )
        return self._restore_cpu()

    def _restore_cpu(self) -> GpuStatus:
        """Reinstall CPU onnxruntime after a failed GPU swap."""
        cpu_restore = self._pip("install", _ORT_CPU_SPEC)
        self._clear_module_cache()
        if cpu_restore.returncode != 0:
            logger.error(
                "CPU onnxruntime restore also failed (rc=%d)", cpu_restore.returncode
            )
            return GpuStatus.RESTORE_FAILED
        return GpuStatus.RESTORED

    def _pip(self, action: str, spec: str) -> subprocess.CompletedProcess[bytes]:
        """Run ``uv pip <action> --python <python> <spec>`` and return the result."""
        return subprocess.run(
            [self._uv_path, "pip", action, "--python", self._python, spec],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )

    @staticmethod
    def _clear_module_cache() -> None:
        """Drop cached ``onnxruntime`` so later imports see the swapped package."""
        sys.modules.pop("onnxruntime", None)
