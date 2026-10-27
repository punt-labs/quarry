# ruff: noqa: S603 — subprocess calls invoke trusted system binaries (uv, nvidia-smi, ldconfig)
"""NVIDIA GPU runtime detection and onnxruntime package swapping.

``quarry install`` calls :meth:`GpuRuntime.ensure` to
swap the CPU-only ``onnxruntime`` wheel for ``onnxruntime-gpu`` when an NVIDIA
GPU is present.  The swap is best-effort: on any failure the CPU runtime is
restored so the daemon still starts.  Safe to call on any platform — it returns
early when ``uv`` or ``nvidia-smi`` is absent (macOS, CPU-only Linux).

The wheel it installs is matched to the host's *loadable* CUDA major.
``onnxruntime-gpu`` links ``libcudart.so.<major>`` at import; the wrong major
makes ``import onnxruntime`` raise, which is strictly worse than the CPU wheel.
So the swap probes ``ldconfig`` for the resolvable CUDA runtime, selects the
matching version range, installs it, and *verifies the result imports* before
declaring success.  A host with no mappable CUDA runtime keeps CPU and warns.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from typing import Final, Self

from quarry.gpu_status import GpuStatus

logger = logging.getLogger(__name__)

_ORT_CPU_SPEC = "onnxruntime>=1.18.0"

# CUDA major -> onnxruntime-gpu version spec whose wheel links that major's
# libcudart. Published compatibility facts, verified against the onnxruntime
# docs' GPU dependency table at implementation time (2026-07):
#   https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html
# - "Starting with version 1.27, GPU packages published to PyPI are built with
#   CUDA 13.0 by default. Older GPU package versions are built with CUDA 12.8."
#   => the <1.27.0 ceiling is the load-bearing CUDA-12 upper bound.
# - The docs' table lists 1.19.x as the first release whose PyPI-default build
#   is CUDA 12 (1.18.x defaults to CUDA 11.8 on PyPI). => the >=1.19.0 floor.
_ORT_GPU_BY_CUDA_MAJOR: Final[dict[int, str]] = {
    12: "onnxruntime-gpu>=1.19.0,<1.27.0",
    13: "onnxruntime-gpu>=1.27.0",
}

# libcudart.so.<major> as printed by ``ldconfig -p`` in the loader cache.
_LIBCUDART_MAJOR_RE = re.compile(r"libcudart\.so\.(\d+)\b")


class GpuRuntime:
    """Swap onnxruntime for onnxruntime-gpu matched to the host CUDA major."""

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
        """Run the detection/swap workflow once ``uv`` is known to be present.

        The pre-swap ``_cuda_available`` probe returns ``False`` when it cannot
        run (an ``OSError`` at the subprocess boundary), so a probe that *could
        not run* is not distinguished from one that ran and found CPU-only — both
        route to ``_swap``. That is safe, not a regression: ``_swap`` first calls
        the OSError-safe ``_detect_cuda_majors``. With no resolvable CUDA runtime
        it returns ``CUDA_UNSUPPORTED`` having mutated nothing; with a resolvable
        major it installs the matched wheel and ``_verify_install`` re-probes,
        restoring CPU on any import failure. So a pre-swap probe boundary failure
        can never leave the environment worse than it started — the worst case is
        a no-op ``CUDA_UNSUPPORTED``, and any real problem is caught by verify.
        """
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
        try:
            result = subprocess.run(
                [nvidia_smi],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except OSError as exc:
            # subprocess.run can raise OSError at the boundary — the nvidia-smi
            # binary removed between the shutil.which check and exec, or a
            # signal at fork/exec. A probe that cannot run means "no usable
            # GPU": return False so _resolve returns NO_GPU without ever
            # touching packages. Narrowly scoped to the subprocess boundary
            # (OSError only), consistent with the ldconfig and provider guards.
            logger.info("nvidia-smi probe raised %s — no usable NVIDIA GPU", exc)
            return False
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

        ``subprocess.run`` can raise ``OSError`` at the boundary — the
        interpreter binary removed between calls, or a signal at exec. A probe
        that cannot run means "CUDA not confirmed available", so return
        ``False``. Narrowly scoped to the subprocess boundary (``OSError``
        only), this keeps both call sites robust: the pre-swap ``_resolve``
        check and, critically, the post-install ``_verify_install`` re-probe —
        which runs AFTER the CPU wheel is gone, so a raised probe would strand
        the host on an unverified GPU wheel with no fallback. Returning ``False``
        routes ``_verify_install`` to ``_restore_cpu`` as the contract requires.
        """
        try:
            provider_check = subprocess.run(
                [
                    self._python,
                    "-c",
                    (
                        "import onnxruntime; "
                        "print(','.join(onnxruntime.get_available_providers()))"
                    ),
                ],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except OSError as exc:
            logger.warning("CUDA provider probe raised %s — treating as CPU-only", exc)
            return False
        return (
            provider_check.returncode == 0
            and "CUDAExecutionProvider" in provider_check.stdout
        )

    @staticmethod
    def _detect_cuda_majors() -> frozenset[int]:
        """Return the CUDA runtime majors the dynamic loader can resolve.

        Parses ``ldconfig -p`` (the loader cache) for every ``libcudart.so.N``
        SONAME — the exact library ``onnxruntime-gpu`` loads at import — and
        returns the set of majors ``N``. This reads the *loadable* runtime, not
        the driver's maximum-supported CUDA (a ceiling), so a host that supports
        CUDA 13 but has only ``libcudart.so.12`` on the path yields ``{12}``.

        Returns the empty set when ``ldconfig`` is absent or lists no
        ``libcudart`` — a meaningful "no CUDA runtime resolvable" result that
        the caller handles explicitly, never a guess.
        """
        ldconfig = shutil.which("ldconfig")
        if ldconfig is None:
            logger.info("ldconfig not on PATH — cannot resolve CUDA runtime")
            return frozenset()
        try:
            listing = subprocess.run(
                [ldconfig, "-p"],
                capture_output=True,
                # Linux library paths are byte strings, not guaranteed UTF-8.
                # Decode leniently so a single mangled path can't raise
                # UnicodeDecodeError out of the swap and become a hard install
                # failure — the bad line simply fails the libcudart regex and
                # the good lines still parse.
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except OSError as exc:
            # subprocess.run can raise OSError at the boundary — the ldconfig
            # binary removed between the shutil.which check and exec, or a
            # signal at fork/exec. Treat that as the same "no CUDA runtime
            # resolvable" fallback as the ldconfig-missing case: keep CPU, warn,
            # never crash the install. Narrowly scoped to the subprocess
            # boundary (OSError only), so this is PL-PP-3 compliant.
            logger.warning(
                "ldconfig probe raised %s — cannot resolve CUDA runtime, keeping CPU",
                exc,
            )
            return frozenset()
        if listing.returncode != 0:
            # A crashed probe (permission denied, corrupt cache) yields empty
            # stdout — indistinguishable from a CUDA-less host if we stay silent.
            # Warn with the return code so the empty result is visible, then keep
            # CPU. This is a DISTINCT signal from _swap's "no matching build".
            logger.warning(
                "ldconfig exited %d (%s) — cannot probe CUDA runtime, keeping CPU",
                listing.returncode,
                listing.stderr.strip(),
            )
            return frozenset()
        majors = {int(m) for m in _LIBCUDART_MAJOR_RE.findall(listing.stdout)}
        return frozenset(majors)

    @staticmethod
    def _select_gpu_spec(majors: frozenset[int]) -> str | None:
        """Return the onnxruntime-gpu spec for the host, or ``None`` if none fits.

        Picks the highest detected major that the compatibility table maps: if a
        host has both ``libcudart.so.12`` and ``.so.13`` loadable, the newer
        onnxruntime line is preferable and valid. ``None`` is the documented "no
        supported CUDA runtime" contract (empty detection, or only majors with
        no onnxruntime line yet, e.g. a future CUDA 14) that drives the
        fail-loud branch in :meth:`_swap` — a discriminated state, not a
        give-up value (PY-EH-8 / PY-TS-14).
        """
        mappable = majors & _ORT_GPU_BY_CUDA_MAJOR.keys()
        if not mappable:
            return None
        return _ORT_GPU_BY_CUDA_MAJOR[max(mappable)]

    def _swap(self) -> GpuStatus:
        """Install the CUDA-matched onnxruntime-gpu, verifying it imports.

        Thin orchestrator: detect the loadable CUDA majors, select the matching
        spec (no match ⇒ keep CPU and warn), replace the CPU wheel, then re-probe
        the installed wheel — success is "onnxruntime-gpu imports with CUDA", not
        "pip exited 0". Any failure restores the CPU runtime.
        """
        majors = self._detect_cuda_majors()
        spec = self._select_gpu_spec(majors)
        if spec is None:
            logger.warning(
                "no onnxruntime-gpu build matches the host CUDA runtime "
                "(detected majors=%s, supported=%s) — keeping CPU onnxruntime",
                sorted(majors),
                sorted(_ORT_GPU_BY_CUDA_MAJOR),
            )
            return GpuStatus.CUDA_UNSUPPORTED
        logger.info("Swapping onnxruntime for %s (python=%s)", spec, self._python)
        try:
            # Uninstall CPU onnxruntime (suppress errors — may not be installed),
            # then install the CUDA-matched wheel: onnxruntime and onnxruntime-gpu
            # are separate PyPI distributions of the same import package, so the
            # outgoing wheel must go before the incoming one or they shadow each
            # other. subprocess.run can raise OSError at this boundary — the uv
            # binary removed between the shutil.which check and exec, or a signal.
            # Treat that identically to a non-zero return code: restore CPU.
            # Narrowly scoped to the subprocess boundary, so this is not defensive.
            self._pip("uninstall", "onnxruntime")
            gpu_install = self._pip("install", spec)
        except OSError as exc:
            logger.warning(
                "onnxruntime-gpu install raised %s, restoring CPU runtime", exc
            )
            return self._restore_cpu()
        if gpu_install.returncode != 0:
            logger.warning(
                "onnxruntime-gpu install failed (rc=%d), restoring CPU runtime",
                gpu_install.returncode,
            )
            return self._restore_cpu()
        return self._verify_install(spec)

    def _verify_install(self, spec: str) -> GpuStatus:
        """Confirm the freshly-installed onnxruntime-gpu imports with CUDA.

        A clean ``pip install`` only means the wheel unpacked; a CUDA-major
        mismatch is an *import-time* failure. Re-probe with :meth:`_cuda_available`
        (a fresh subprocess, avoiding stale ``.so`` state). If it imports with
        CUDA the swap is done; otherwise the wheel does not run here — restore
        CPU rather than leave the daemon with an unimportable onnxruntime.
        """
        self._clear_module_cache()
        if self._cuda_available():
            logger.info("onnxruntime-gpu installed and verified (%s)", spec)
            return GpuStatus.INSTALLED
        logger.warning(
            "onnxruntime-gpu (%s) installed but does not import with CUDA here, "
            "restoring CPU runtime",
            spec,
        )
        return self._restore_cpu()

    def _restore_cpu(self) -> GpuStatus:
        """Uninstall onnxruntime-gpu, then reinstall CPU onnxruntime.

        The restore is the last-resort path: every ``_swap`` failure branch and
        the ``_verify_install`` import-failure branch route through here to end
        on a defined :class:`GpuStatus`. ``onnxruntime`` and ``onnxruntime-gpu``
        are SEPARATE PyPI distributions that both provide the ``onnxruntime``
        import package; installed together they shadow each other and
        ``import onnxruntime`` can still fail. ``_verify_install`` reaches here
        precisely when the GPU wheel installed cleanly (``pip`` rc 0) but did
        not import — so the GPU wheel is present and MUST be uninstalled first,
        mirroring how ``_swap`` uninstalls the CPU wheel before the GPU one.
        The uninstall is best-effort (rc suppressed) — the GPU wheel may be
        absent (the ``_swap`` early-failure branches route here before it
        installs), and a missing package must not fail the restore.

        ``subprocess.run`` can itself raise ``OSError`` at the boundary — the
        same race that motivates the guard in ``_swap`` (the ``uv`` binary
        removed between the ``shutil.which`` check and exec, or a signal). Both
        the uninstall and the install run inside this one guarded region, so an
        ``OSError`` from either yields ``RESTORE_FAILED`` and never escapes.
        ``RESTORE_FAILED`` already means "GPU install failed AND CPU restore
        failed", which is exactly this state.
        """
        try:
            # Uninstall the lingering GPU wheel (suppress rc — may be absent),
            # then install the CPU wheel so import onnxruntime resolves cleanly.
            self._pip("uninstall", "onnxruntime-gpu")
            cpu_restore = self._pip("install", _ORT_CPU_SPEC)
        except OSError as exc:
            logger.error("CPU onnxruntime restore raised %s — restore failed", exc)
            return GpuStatus.RESTORE_FAILED
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
