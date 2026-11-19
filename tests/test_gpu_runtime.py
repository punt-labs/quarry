"""Tests for quarry.gpu_runtime — NVIDIA GPU detection and onnxruntime swap.

The swap is subprocess orchestration, so it is exercised by asserting on the
argv issued to ``nvidia-smi``, ``ldconfig -p``, and ``uv pip`` — these are the
real system-boundary calls, not ML mocks. Each test declares the host's CUDA
runtime through the ``ldconfig`` branch of the shared ``_default_run`` mock.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from quarry.gpu_runtime import GpuRuntime
from quarry.gpu_status import GpuStatus

if TYPE_CHECKING:
    import pytest


def _which(present: Sequence[str]) -> Callable[[str], str | None]:
    """Build a ``shutil.which`` side effect where ``present`` names resolve."""

    def which_side_effect(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in present else None

    return which_side_effect


def _ldconfig_stdout(majors: Sequence[int]) -> str:
    """Render an ``ldconfig -p`` listing exposing ``libcudart.so.<major>``."""
    lines = [
        f"\tlibcudart.so.{m} (libc6,x86-64) => /lib/x86_64-linux-gnu/libcudart.so.{m}"
        for m in majors
    ]
    return "\n".join(["\t1234 libs found in cache '/etc/ld.so.cache'", *lines, ""])


def _is_provider_check(cmd: Sequence[str]) -> bool:
    """Return whether ``cmd`` is the in-subprocess onnxruntime provider probe."""
    return len(cmd) > 0 and cmd[0] == sys.executable and "-c" in cmd


def _is_ldconfig(cmd: Sequence[str]) -> bool:
    """Return whether ``cmd`` is the ``ldconfig -p`` loader-cache probe."""
    return len(cmd) >= 2 and cmd[0].endswith("ldconfig") and cmd[1] == "-p"


def _pip_install_specs(calls: Sequence[Sequence[str]]) -> list[str]:
    """Extract the package spec from every ``uv pip install <spec>`` call."""
    return [str(cmd[-1]) for cmd in calls if "pip" in cmd and "install" in cmd]


def _pip_pkg_ops(calls: Sequence[Sequence[str]]) -> list[tuple[str, str]]:
    """Extract ``(action, spec)`` pairs from every ``uv pip <action> <spec>``.

    Captures both ``install`` and ``uninstall`` so a test can assert the RELATIVE
    order of an uninstall against an install — ``_pip_install_specs`` sees only
    installs and cannot express "uninstall X came before install Y".
    """
    ops: list[tuple[str, str]] = []
    for cmd in calls:
        if "pip" not in cmd:
            continue
        if "install" in cmd:
            ops.append(("install", str(cmd[-1])))
        elif "uninstall" in cmd:
            ops.append(("uninstall", str(cmd[-1])))
    return ops


class _RunState:
    """Mutable flag tracking whether the GPU wheel install has run yet.

    The provider check runs twice with identical argv (once before the swap,
    once to verify the freshly-installed wheel). This lets the mock answer
    "CPU only" the first time and "CUDA available" after the install.
    """

    _installed: bool

    def __new__(cls) -> _RunState:
        self = super().__new__(cls)
        self._installed = False
        return self

    def mark_installed(self) -> None:
        """Record that a ``uv pip install`` of the GPU wheel has completed."""
        self._installed = True

    @property
    def installed(self) -> bool:
        """Return whether the GPU wheel install has run."""
        return self._installed


def _default_run(
    cmd: Sequence[str],
    *,
    ldconfig_majors: Sequence[int],
    cuda_after_install: bool,
    state: _RunState | None = None,
) -> MagicMock:
    """Default ``subprocess.run`` side effect for a healthy swap sequence.

    ``ldconfig_majors`` declares the host's loadable CUDA runtimes. The provider
    check reports CPU-only until the GPU wheel installs; after install it
    reports CUDA when ``cuda_after_install`` is set.
    """
    if _is_ldconfig(cmd):
        return MagicMock(returncode=0, stdout=_ldconfig_stdout(ldconfig_majors))
    if _is_provider_check(cmd):
        post_install = state.installed if state is not None else True
        cuda_up = cuda_after_install and post_install
        providers = (
            "CUDAExecutionProvider,CPUExecutionProvider"
            if cuda_up
            else "CPUExecutionProvider"
        )
        return MagicMock(returncode=0, stdout=f"{providers}\n")
    if state is not None and "pip" in cmd and "install" in cmd:
        state.mark_installed()
    return MagicMock(returncode=0)


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

    def test_cuda12_host_selects_cuda12_wheel(self) -> None:
        """A CUDA-12 host installs the CUDA-12-matched onnxruntime-gpu spec.

        An unconstrained GPU wheel can require CUDA 13's ``libcudart.so.13``,
        which is unimportable on a CUDA-12 host. Detect ``libcudart.so.12`` and
        select a compatible wheel.
        """
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        state = _RunState()
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            return _default_run(
                cmd, ldconfig_majors=(12,), cuda_after_install=True, state=state
            )

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        assert result == GpuStatus.INSTALLED
        install_specs = _pip_install_specs(calls)
        # The CUDA-12-matched spec is installed …
        assert "onnxruntime-gpu>=1.19.0,<1.27.0" in install_specs
        # … and NOT the old unbounded spec (which resolves to the CUDA-13 1.27.0
        # wheel) nor the CUDA-13 spec whose floor is 1.27.0.
        assert "onnxruntime-gpu>=1.18.0" not in install_specs
        assert "onnxruntime-gpu>=1.27.0" not in install_specs

    def test_swap_success(self) -> None:
        """When nvidia-smi works and CUDA not available, swap succeeds."""
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        state = _RunState()
        call_count = 0

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            return _default_run(
                cmd, ldconfig_majors=(12,), cuda_after_install=True, state=state
            )

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch(
                "quarry.gpu_runtime.subprocess.run",
                side_effect=run_side_effect,
            ),
        ):
            result = GpuRuntime.ensure()

        assert result == "onnxruntime-gpu installed"
        # nvidia-smi + provider check + ldconfig + uninstall + install +
        # post-install verify = 6 subprocess calls.
        assert call_count == 6

    def test_swap_failure_restores_cpu(self) -> None:
        """When the CUDA-matched onnxruntime-gpu install fails, CPU is restored."""
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            if _is_ldconfig(cmd):
                return MagicMock(returncode=0, stdout=_ldconfig_stdout((12,)))
            if _is_provider_check(cmd):
                return MagicMock(returncode=0, stdout="CPUExecutionProvider\n")
            # nvidia-smi OK, uninstall OK, gpu install fails, cpu restore OK.
            if "onnxruntime-gpu>=1.19.0,<1.27.0" in cmd:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        assert result == "onnxruntime-gpu install failed, CPU restored"
        # CPU restore was issued exactly once.
        restore_calls = [c for c in calls if "onnxruntime>=1.18.0" in c]
        assert len(restore_calls) == 1
        # Return value distinguishes from the "restore also failed" case.
        assert "also failed" not in result
        # Order matters: the GPU-install attempt precedes the CPU restore, so
        # the restore is the last package op and leaves the CPU wheel installed.
        install_specs = _pip_install_specs(calls)
        assert install_specs.index("onnxruntime-gpu>=1.19.0,<1.27.0") < (
            install_specs.index("onnxruntime>=1.18.0")
        )

    def test_swap_failure_restore_also_fails(self) -> None:
        """When both GPU install and CPU restore fail, return a distinct message."""
        which = _which(["uv", "nvidia-smi", "ldconfig"])

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if _is_ldconfig(cmd):
                return MagicMock(returncode=0, stdout=_ldconfig_stdout((12,)))
            if _is_provider_check(cmd):
                return MagicMock(returncode=0, stdout="CPUExecutionProvider\n")
            # nvidia-smi OK, uninstall OK, gpu install fails, cpu restore fails.
            if "onnxruntime-gpu>=1.19.0,<1.27.0" in cmd:
                return MagicMock(returncode=1)
            if "onnxruntime>=1.18.0" in cmd:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        assert result == "onnxruntime-gpu install failed, CPU restore also failed"

    def test_swap_success_clears_module_cache(self) -> None:
        """After a successful swap, 'onnxruntime' must not remain in sys.modules."""
        import sys as _sys

        which = _which(["uv", "nvidia-smi", "ldconfig"])
        state = _RunState()
        mock_ort = MagicMock()

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            return _default_run(
                cmd, ldconfig_majors=(12,), cuda_after_install=True, state=state
            )

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
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

    def test_cuda13_host_selects_cuda13_wheel(self) -> None:
        """A CUDA-13 host installs the CUDA-13 line ``onnxruntime-gpu>=1.27.0``."""
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        state = _RunState()
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            return _default_run(
                cmd, ldconfig_majors=(13,), cuda_after_install=True, state=state
            )

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        assert result == GpuStatus.INSTALLED
        install_specs = _pip_install_specs(calls)
        assert "onnxruntime-gpu>=1.27.0" in install_specs
        assert "onnxruntime-gpu>=1.19.0,<1.27.0" not in install_specs

    def test_both_cuda_majors_picks_highest(self) -> None:
        """With both libcudart.so.12 and .so.13 loadable, the newer line wins."""
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        state = _RunState()
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            return _default_run(
                cmd, ldconfig_majors=(12, 13), cuda_after_install=True, state=state
            )

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        assert result == GpuStatus.INSTALLED
        install_specs = _pip_install_specs(calls)
        # Highest mappable major (13) selected, not the 12 line.
        assert "onnxruntime-gpu>=1.27.0" in install_specs
        assert "onnxruntime-gpu>=1.19.0,<1.27.0" not in install_specs

    def test_gpu_present_no_libcudart_keeps_cpu(self) -> None:
        """GPU present but no libcudart on the loader path → CUDA_UNSUPPORTED.

        The ``ldconfig -p`` cache lists no ``libcudart.so.N``. No
        ``onnxruntime-gpu`` build can be proven to import, so none is installed
        and the working CPU runtime is kept (a recovered warning, not a failure).
        """
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            return _default_run(cmd, ldconfig_majors=(), cuda_after_install=False)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        assert result == GpuStatus.CUDA_UNSUPPORTED
        assert result.is_recovered is True
        assert result.is_failure is False
        # No onnxruntime-gpu install command was issued.
        assert not any("onnxruntime-gpu" in spec for spec in _pip_install_specs(calls))

    def test_unknown_cuda_major_does_not_mispin(self) -> None:
        """A CUDA major with no table entry (14) must not fall back to 12/13.

        ``ldconfig`` reports only ``libcudart.so.14``. The selector has no
        mapping for 14, so it must keep CPU and warn — never silently mis-pin to
        the 12 or 13 spec. This is the "don't guess" guarantee.
        """
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            return _default_run(cmd, ldconfig_majors=(14,), cuda_after_install=False)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        assert result == GpuStatus.CUDA_UNSUPPORTED
        # Crucially: no onnxruntime-gpu spec of any CUDA line was installed.
        assert not any("onnxruntime-gpu" in spec for spec in _pip_install_specs(calls))
        # The status renders "running on CPU" — the display consumed by doctor.
        assert "running on CPU" in result

    def test_ldconfig_absent_keeps_cpu(self) -> None:
        """GPU present but ``ldconfig`` not on PATH → CUDA_UNSUPPORTED, keep CPU.

        Guards the boundary where the probe tool itself is missing: detection
        yields the empty set and the swap fails loud rather than guessing.
        """
        # Note: ldconfig NOT in the resolvable set.
        which = _which(["uv", "nvidia-smi"])
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            if _is_provider_check(cmd):
                return MagicMock(returncode=0, stdout="CPUExecutionProvider\n")
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        assert result == GpuStatus.CUDA_UNSUPPORTED
        # ldconfig was never invoked (shutil.which returned None for it) …
        assert not any(_is_ldconfig(c) for c in calls)
        # … and no onnxruntime-gpu install was issued.
        assert not any("onnxruntime-gpu" in spec for spec in _pip_install_specs(calls))

    def test_ldconfig_nonzero_exit_warns_with_returncode(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``ldconfig -p`` exiting non-zero → CUDA_UNSUPPORTED + a distinct warning.

        A failed ``ldconfig`` (permission denied, corrupt cache) yields empty
        stdout and thus an empty major set — indistinguishable from "no CUDA" if
        we stay silent. The run must log a warning that NAMES the return code,
        so an operator can tell a crashed probe from a genuinely CUDA-less host.
        Detection still returns the empty set (keep CPU), never a guess.
        """
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            if _is_ldconfig(cmd):
                # Non-zero exit with empty stdout, stderr names the cause.
                return MagicMock(returncode=13, stdout="", stderr="permission denied")
            if _is_provider_check(cmd):
                return MagicMock(returncode=0, stdout="CPUExecutionProvider\n")
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
            caplog.at_level(logging.WARNING, logger="quarry.gpu_runtime"),
        ):
            result = GpuRuntime.ensure()

        assert result == GpuStatus.CUDA_UNSUPPORTED
        # A distinct warning names the return code — not the generic
        # "no matching build (detected=[])" message that masks the crash.
        ldconfig_warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "ldconfig" in r.getMessage()
        ]
        assert any("13" in msg for msg in ldconfig_warnings)
        # No onnxruntime-gpu install was issued (keep CPU).
        assert not any("onnxruntime-gpu" in spec for spec in _pip_install_specs(calls))

    def test_ldconfig_non_utf8_output_does_not_raise(self) -> None:
        """A non-UTF-8 byte in the ``ldconfig -p`` listing must not crash the swap.

        Linux library paths are byte strings, not guaranteed UTF-8. With
        ``errors="replace"`` on the subprocess decode, a mangled line simply
        fails the ``libcudart`` regex; the clean ``libcudart.so.12`` line is
        still parsed, so the CUDA-12 wheel installs instead of the whole swap
        exploding into a hard install failure (UnicodeDecodeError propagating
        out of ``ensure()``).
        """
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        state = _RunState()
        seen_kwargs: dict[str, object] = {}
        # A garbled path where the raw bytes were 0xff; errors="replace"
        # substitutes U+FFFD. The libcudart.so.12 line survives intact.
        hostile_stdout = (
            "\t1234 libs found in cache '/etc/ld.so.cache'\n"
            "\tlibgarbled.so.1 (libc6) => /lib/��/libgarbled.so.1\n"
            "\tlibcudart.so.12 (libc6,x86-64) => /lib/libcudart.so.12\n"
        )

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            if _is_ldconfig(cmd):
                # Capture the decode kwargs the swap passes to subprocess.run.
                seen_kwargs.update(kwargs)
                return MagicMock(returncode=0, stdout=hostile_stdout)
            return _default_run(
                cmd, ldconfig_majors=(12,), cuda_after_install=True, state=state
            )

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        # No UnicodeDecodeError propagated; the CUDA-12 major was still parsed.
        assert result == GpuStatus.INSTALLED
        # The decode is lossy-tolerant: errors="replace" so a bad byte never
        # raises out of _detect_cuda_majors.
        assert seen_kwargs.get("encoding") == "utf-8"
        assert seen_kwargs.get("errors") == "replace"

    def test_install_succeeds_but_import_fails_restores_cpu(self) -> None:
        """A clean pip install that fails to import is caught → RESTORED.

        The CUDA-12 wheel installs (rc 0) but the post-install provider re-probe
        returns non-zero (simulating the ``libcudart`` ImportError). This must
        NOT be reported as INSTALLED: CPU is restored and the status is RESTORED,
        distinct from CUDA_UNSUPPORTED (a wheel WAS installed, then rolled back).
        """
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            if _is_ldconfig(cmd):
                return MagicMock(returncode=0, stdout=_ldconfig_stdout((12,)))
            if _is_provider_check(cmd):
                # Both the pre-swap and post-install probes fail to expose CUDA:
                # pre-swap CPU-only (swap needed), post-install import fails.
                return MagicMock(returncode=1, stdout="")
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        # RESTORED, never INSTALLED — a clean pip install that fails to import
        # must not be mistaken for success.
        assert result == GpuStatus.RESTORED
        install_specs = _pip_install_specs(calls)
        # The GPU wheel WAS installed (distinguishing from CUDA_UNSUPPORTED) …
        assert "onnxruntime-gpu>=1.19.0,<1.27.0" in install_specs
        # … then CPU was restored after the failed import re-probe.
        assert "onnxruntime>=1.18.0" in install_specs
        # Order matters: the GPU wheel is installed BEFORE the CPU restore.
        # A restore that ran first would leave the daemon on the GPU wheel it
        # could not import — the exact failure the rollback exists to prevent.
        assert install_specs.index("onnxruntime-gpu>=1.19.0,<1.27.0") < (
            install_specs.index("onnxruntime>=1.18.0")
        )
        # The GPU wheel installed cleanly (rc 0) but failed import, so it lingers
        # alongside the CPU wheel — both are separate PyPI distributions of the
        # same import package and shadow each other. The restore MUST uninstall
        # onnxruntime-gpu BEFORE installing the CPU spec, or import onnxruntime
        # can still fail and the RESTORED status would be a false "recovered".
        pkg_ops = _pip_pkg_ops(calls)
        assert ("uninstall", "onnxruntime-gpu") in pkg_ops
        assert pkg_ops.index(("uninstall", "onnxruntime-gpu")) < (
            pkg_ops.index(("install", "onnxruntime>=1.18.0"))
        )

    def test_verify_probe_oserror_restores_cpu(self) -> None:
        """An ``OSError`` from the post-install verify probe restores CPU.

        The GPU wheel installs cleanly (rc 0), then ``_verify_install`` re-probes
        with ``_cuda_available`` — a fresh subprocess that can itself raise
        ``OSError`` (the interpreter binary removed, a signal at exec). If that
        raises, it must NOT escape ``_swap``/``ensure`` and strand the host on an
        unverified GPU wheel with no CPU fallback. ``_cuda_available`` treats the
        boundary failure as "CUDA not confirmed available" (``False``), so the
        post-install probe routes to the CPU restore: the result is RESTORED and
        no exception propagates.
        """
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        state = _RunState()
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            if _is_ldconfig(cmd):
                return MagicMock(returncode=0, stdout=_ldconfig_stdout((12,)))
            if _is_provider_check(cmd):
                # Pre-swap probe: CPU only (swap needed). Post-install probe:
                # the exec raises OSError at the subprocess boundary.
                if state.installed:
                    raise OSError("interpreter vanished during verify probe")
                return MagicMock(returncode=0, stdout="CPUExecutionProvider\n")
            if "pip" in cmd and "install" in cmd:
                state.mark_installed()
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        # The verify-probe OSError did not propagate; CPU was restored.
        assert result == GpuStatus.RESTORED
        install_specs = _pip_install_specs(calls)
        # The GPU wheel WAS installed, then rolled back to CPU after the probe
        # could not confirm CUDA.
        assert "onnxruntime-gpu>=1.19.0,<1.27.0" in install_specs
        assert "onnxruntime>=1.18.0" in install_specs
        assert install_specs.index("onnxruntime-gpu>=1.19.0,<1.27.0") < (
            install_specs.index("onnxruntime>=1.18.0")
        )

    def test_uninstall_oserror_restores_cpu(self) -> None:
        """An ``OSError`` from the ``_swap`` CPU uninstall restores CPU, not crashes.

        ``subprocess.run`` can raise ``OSError`` at the boundary — the ``uv``
        binary vanishing between the ``shutil.which`` check and exec, or a
        signal. If the CPU-onnxruntime UNINSTALL in ``_swap`` raises, the swap
        must treat that identically to a non-zero return code and fall through to
        the CPU restore, honouring the "restore CPU on any failure" contract
        instead of propagating. The OSError is scoped to the ``_swap`` CPU
        uninstall (``onnxruntime``); the restore's own ``onnxruntime-gpu``
        uninstall runs normally, so the restore succeeds → RESTORED.
        """
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            if _is_ldconfig(cmd):
                return MagicMock(returncode=0, stdout=_ldconfig_stdout((12,)))
            if _is_provider_check(cmd):
                return MagicMock(returncode=0, stdout="CPUExecutionProvider\n")
            if "uninstall" in cmd and "onnxruntime" in cmd:
                # The uv binary vanished mid-run; exec raises before the child.
                raise OSError("uv binary vanished")
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        # The OSError did not propagate; CPU was restored.
        assert result == GpuStatus.RESTORED
        install_specs = _pip_install_specs(calls)
        assert "onnxruntime>=1.18.0" in install_specs
        # The GPU wheel was never installed — the uninstall raised before it.
        assert not any("onnxruntime-gpu" in spec for spec in install_specs)

    def test_gpu_install_oserror_restores_cpu(self) -> None:
        """An ``OSError`` from the GPU-install subprocess also restores CPU.

        The uninstall succeeds, then the ``uv pip install`` of the GPU wheel
        raises ``OSError`` (binary removed, signal). Same contract as a non-zero
        return: fall through to the CPU restore and return RESTORED.
        """
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            if _is_ldconfig(cmd):
                return MagicMock(returncode=0, stdout=_ldconfig_stdout((12,)))
            if _is_provider_check(cmd):
                return MagicMock(returncode=0, stdout="CPUExecutionProvider\n")
            if "install" in cmd and "onnxruntime-gpu>=1.19.0,<1.27.0" in cmd:
                raise OSError("uv binary vanished")
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        assert result == GpuStatus.RESTORED
        install_specs = _pip_install_specs(calls)
        # The CPU wheel was restored despite the GPU install raising.
        assert "onnxruntime>=1.18.0" in install_specs

    def test_ldconfig_exec_oserror_keeps_cpu(self) -> None:
        """An ``OSError`` from the ``ldconfig -p`` exec → CUDA_UNSUPPORTED, keep CPU.

        ``shutil.which`` resolves ``ldconfig``, but ``subprocess.run`` raises
        ``OSError`` at the boundary (the binary removed between the check and
        exec, or a signal at fork). ``_detect_cuda_majors`` must absorb that as
        the same "no CUDA runtime resolvable" fallback as the ldconfig-missing
        case: return the empty set, so the swap keeps CPU and warns rather than
        letting the OSError escape ``ensure()`` and crash the install.
        """
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            if _is_ldconfig(cmd):
                raise OSError("ldconfig binary vanished before exec")
            if _is_provider_check(cmd):
                return MagicMock(returncode=0, stdout="CPUExecutionProvider\n")
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        # The OSError did not propagate; the host keeps CPU as a recovered warning.
        assert result == GpuStatus.CUDA_UNSUPPORTED
        assert result.is_recovered is True
        assert result.is_failure is False
        # No onnxruntime-gpu install was issued — nothing was mutated.
        assert not any("onnxruntime-gpu" in spec for spec in _pip_install_specs(calls))

    def test_nvidia_smi_exec_oserror_no_mutation(self) -> None:
        """An ``OSError`` from the ``nvidia-smi`` exec → NO_GPU, packages untouched.

        ``shutil.which`` resolves ``nvidia-smi`` but ``subprocess.run`` raises
        ``OSError`` (binary removed between the check and exec, or a signal at
        fork). A probe that cannot run means "no usable GPU": ``_resolve`` must
        return ``NO_GPU`` and never reach ``_swap``, so no ldconfig probe and no
        pip command run. This preempts the same boundary crash the ldconfig and
        provider guards already close.
        """
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            if cmd[0].endswith("nvidia-smi"):
                raise OSError("nvidia-smi binary vanished before exec")
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        # The OSError did not propagate; detection reports no GPU.
        assert result == GpuStatus.NO_GPU
        # No swap workflow ran: no ldconfig probe and no package operation.
        assert not any(_is_ldconfig(c) for c in calls)
        assert _pip_install_specs(calls) == []

    def test_pre_swap_probe_oserror_no_worse_state(self) -> None:
        """A pre-swap ``_cuda_available`` OSError leaves the env no worse off.

        ``shutil.which`` resolves everything and ``nvidia-smi`` reports a GPU,
        but the PRE-swap provider probe raises ``OSError`` at the boundary.
        ``_cuda_available`` returns ``False`` (probe could not run), so
        ``_resolve`` proceeds to ``_swap``. With a resolvable CUDA-12 runtime the
        swap installs the matched wheel and the POST-swap verify probe (which
        does NOT raise here) confirms CUDA → INSTALLED. The point of the test:
        the pre-swap boundary failure did not abort the install nor strand a
        half-swapped env — it either succeeds cleanly or (see the empty-majors
        variant) no-ops to CUDA_UNSUPPORTED, never worse than the start state.
        """
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        state = _RunState()
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            if _is_ldconfig(cmd):
                return MagicMock(returncode=0, stdout=_ldconfig_stdout((12,)))
            if _is_provider_check(cmd):
                # Pre-swap probe raises; post-install probe reports CUDA up.
                if not state.installed:
                    raise OSError("interpreter vanished during pre-swap probe")
                return MagicMock(
                    returncode=0,
                    stdout="CUDAExecutionProvider,CPUExecutionProvider\n",
                )
            if "pip" in cmd and "install" in cmd:
                state.mark_installed()
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        # The pre-swap OSError did not propagate; the swap proceeded and verified.
        assert result == GpuStatus.INSTALLED
        install_specs = _pip_install_specs(calls)
        assert "onnxruntime-gpu>=1.19.0,<1.27.0" in install_specs

    def test_pre_swap_probe_oserror_no_cuda_runtime_is_noop(self) -> None:
        """Pre-swap probe OSError + no resolvable CUDA → CUDA_UNSUPPORTED, no-op.

        The dangerous case the Bugbot HIGH flagged: a pre-swap probe that could
        NOT run (OSError → ``False``) routing to ``_swap`` on a host with no
        loadable ``libcudart``. ``_detect_cuda_majors`` returns the empty set,
        ``_select_gpu_spec`` returns ``None``, and ``_swap`` returns
        CUDA_UNSUPPORTED having mutated NOTHING — no uninstall, no install. The
        env is exactly as it started: the CPU wheel, untouched.
        """
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            if _is_ldconfig(cmd):
                # No libcudart on the loader path → empty major set.
                return MagicMock(returncode=0, stdout=_ldconfig_stdout(()))
            if _is_provider_check(cmd):
                raise OSError("interpreter vanished during pre-swap probe")
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        # Recovered no-op: no package operation of any kind was issued.
        assert result == GpuStatus.CUDA_UNSUPPORTED
        assert result.is_recovered is True
        assert _pip_install_specs(calls) == []
        # Nothing was uninstalled either — the CPU wheel is exactly as it started.
        assert not any("uninstall" in c for c in calls)

    def test_restore_oserror_returns_restore_failed(self) -> None:
        """An ``OSError`` from the CPU-restore subprocess returns RESTORE_FAILED.

        Same boundary race as ``_swap``: after the GPU install fails, the swap
        calls ``_restore_cpu``, whose own ``uv pip install`` of the CPU wheel can
        raise ``OSError`` (the ``uv`` binary removed between the ``shutil.which``
        check and exec, or a signal). The guard inside ``_restore_cpu`` must
        absorb it and end on the defined ``RESTORE_FAILED`` status — the state
        that already means "GPU install failed AND CPU restore failed" — instead
        of propagating out of ``_swap``/``ensure``. This covers BOTH ``_swap``
        call sites at once because they both route through ``_restore_cpu``.
        """
        which = _which(["uv", "nvidia-smi", "ldconfig"])
        calls: list[list[str]] = []

        def run_side_effect(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            if _is_ldconfig(cmd):
                return MagicMock(returncode=0, stdout=_ldconfig_stdout((12,)))
            if _is_provider_check(cmd):
                return MagicMock(returncode=0, stdout="CPUExecutionProvider\n")
            # GPU install fails (rc 1), routing to _restore_cpu; the CPU-restore
            # install then raises OSError at the subprocess boundary.
            if "install" in cmd and "onnxruntime-gpu>=1.19.0,<1.27.0" in cmd:
                return MagicMock(returncode=1)
            if "install" in cmd and "onnxruntime>=1.18.0" in cmd:
                raise OSError("uv binary vanished during CPU restore")
            return MagicMock(returncode=0)

        with (
            patch("quarry.gpu_runtime.shutil.which", side_effect=which),
            patch("quarry.gpu_runtime.subprocess.run", side_effect=run_side_effect),
        ):
            result = GpuRuntime.ensure()

        # The OSError did not propagate; the defined status is RESTORE_FAILED.
        assert result == GpuStatus.RESTORE_FAILED
        # The CPU restore WAS attempted (distinguishing from CUDA_UNSUPPORTED).
        assert "onnxruntime>=1.18.0" in _pip_install_specs(calls)
