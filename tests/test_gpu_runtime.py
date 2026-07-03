"""Tests for quarry.gpu_runtime — NVIDIA GPU detection and onnxruntime swap."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from quarry.gpu_runtime import GpuRuntime, GpuStatus


class TestGpuRuntimeEnsure:
    """Tests for GpuRuntime.ensure() — GPU detection and onnxruntime swap."""

    def test_no_uv_on_path(self) -> None:
        """When uv is not on PATH, return early without any subprocess calls."""
        with patch("quarry.gpu_runtime.shutil.which", return_value=None):
            result = GpuRuntime.ensure()
        assert result == "uv not found, skipped GPU check"
        assert result is GpuStatus.NO_UV

    def test_no_nvidia_smi(self) -> None:
        """When nvidia-smi is absent, return 'no NVIDIA GPU'."""

        def which_side_effect(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            if name == "nvidia-smi":
                return None
            return None

        with patch("quarry.gpu_runtime.shutil.which", side_effect=which_side_effect):
            result = GpuRuntime.ensure()
        assert result == "no NVIDIA GPU"

    def test_nvidia_smi_fails(self) -> None:
        """When nvidia-smi exists but fails, return 'no NVIDIA GPU'."""

        def which_side_effect(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            if name == "nvidia-smi":
                return "/usr/bin/nvidia-smi"
            return None

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which_side_effect),
            patch(
                "quarry.gpu_runtime.subprocess.run",
                return_value=MagicMock(returncode=1),
            ),
        ):
            result = GpuRuntime.ensure()
        assert result == "no NVIDIA GPU"

    def test_cuda_already_available(self) -> None:
        """When CUDAExecutionProvider is already available, return early."""

        def which_side_effect(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            if name == "nvidia-smi":
                return "/usr/bin/nvidia-smi"
            return None

        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            if cmd[0] == "/usr/bin/nvidia-smi":
                return MagicMock(returncode=0)
            # Provider check subprocess — report CUDA available.
            if cmd[0] == sys.executable and "-c" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="CUDAExecutionProvider,CPUExecutionProvider\n",
                )
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which_side_effect),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        assert result == "CUDA already available"
        # nvidia-smi + provider check = 2 subprocess calls, no pip commands.
        assert len(calls) == 2

    def test_swap_success(self) -> None:
        """When nvidia-smi works and CUDA not available, swap succeeds."""

        def which_side_effect(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            if name == "nvidia-smi":
                return "/usr/bin/nvidia-smi"
            return None

        call_count = 0

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            # Provider check subprocess — report CPU only.
            if cmd[0] == sys.executable and "-c" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="CPUExecutionProvider\n",
                )
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which_side_effect),
            patch(
                "quarry.gpu_runtime.subprocess.run",
                side_effect=run_side_effect,
            ),
        ):
            result = GpuRuntime.ensure()

        assert result == "onnxruntime-gpu installed"
        # nvidia-smi + provider check + uninstall + install = 4 subprocess calls
        assert call_count == 4

    def test_swap_failure_restores_cpu(self) -> None:
        """When onnxruntime-gpu install fails, CPU onnxruntime is restored."""

        def which_side_effect(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            if name == "nvidia-smi":
                return "/usr/bin/nvidia-smi"
            return None

        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(cmd)
            # Provider check — CPU only.
            if cmd[0] == sys.executable and "-c" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="CPUExecutionProvider\n",
                )
            # nvidia-smi OK, uninstall OK, gpu fails, cpu restore OK
            if "onnxruntime-gpu>=1.18.0" in cmd:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which_side_effect),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        assert result == "onnxruntime-gpu install failed, CPU restored"
        # Verify CPU restore was called
        restore_calls = [c for c in calls if "onnxruntime>=1.18.0" in c]
        assert len(restore_calls) == 1
        # Return value distinguishes from the "restore also failed" case.
        assert "also failed" not in result

    def test_swap_failure_restore_also_fails(self) -> None:
        """When both GPU install and CPU restore fail, return a distinct message."""

        def which_side_effect(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            if name == "nvidia-smi":
                return "/usr/bin/nvidia-smi"
            return None

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            # Provider check — CPU only.
            if cmd[0] == sys.executable and "-c" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="CPUExecutionProvider\n",
                )
            # nvidia-smi OK, uninstall OK, gpu install fails, cpu restore fails
            if "onnxruntime-gpu>=1.18.0" in cmd:
                return MagicMock(returncode=1)
            if "onnxruntime>=1.18.0" in cmd:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which_side_effect),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        assert result == "onnxruntime-gpu install failed, CPU restore also failed"

    def test_swap_success_clears_module_cache(self) -> None:
        """After a successful swap, 'onnxruntime' must not remain in sys.modules."""
        import sys as _sys

        def which_side_effect(name: str) -> str | None:
            if name == "uv":
                return "/usr/bin/uv"
            if name == "nvidia-smi":
                return "/usr/bin/nvidia-smi"
            return None

        mock_ort = MagicMock()

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            # Provider check subprocess — report CPU only.
            if cmd[0] == sys.executable and "-c" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="CPUExecutionProvider\n",
                )
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which_side_effect),
            patch(
                "quarry.gpu_runtime.subprocess.run",
                side_effect=run_side_effect,
            ),
            patch.dict("sys.modules", {"onnxruntime": mock_ort}),
        ):
            result = GpuRuntime.ensure()
            # Assert inside the patch.dict context — on exit it restores
            # the original sys.modules state, which would re-add the key.
            assert "onnxruntime" not in _sys.modules

        assert result == "onnxruntime-gpu installed"
