"""Thread-pool limits for ONNX inference under concurrent quarry processes.

Concurrent quarry processes (serve daemon, ingest worker, CLI) each default to
ncpu rayon + ncpu ONNX threads — three on 8 cores reach load ~148 and starve the
query path.  ``ThreadConfig`` caps the budget per hardware/provider: GPU offloads
GEMMs to CUDA (1 feeder thread), CPU caps at 2 (DES-027 arena).  See DES-032.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, Self

logger = logging.getLogger(__name__)

_MAX_CPU_THREADS = 2
_NCPU_NONE = 4  # fallback when os.cpu_count() returns None


class _SessionOptions(Protocol):
    """The ONNX ``SessionOptions`` thread knobs, typed without importing it."""

    intra_op_num_threads: int
    inter_op_num_threads: int


class ThreadConfig:
    """Hardware/provider-derived thread budget for one ONNX session."""

    _ncpu: int
    _intra_op_threads: int
    _omp_threads: int

    def __new__(cls, *, is_gpu: bool) -> Self:
        self = super().__new__(cls)
        if (detected := os.cpu_count()) is None:
            logger.warning("os.cpu_count() returned None; assuming %d CPUs", _NCPU_NONE)
        self._ncpu = detected or _NCPU_NONE
        self._omp_threads = min(_MAX_CPU_THREADS, self._ncpu)
        # GPU does the GEMMs (1 feeder thread); CPU caps at 2 (DES-027 arena).
        self._intra_op_threads = 1 if is_gpu else min(_MAX_CPU_THREADS, self._ncpu)
        return self

    @classmethod
    def for_provider(cls, provider: str) -> Self:
        """Build a budget for an ONNX provider name (e.g. CUDAExecutionProvider)."""
        return cls(is_gpu=provider == "CUDAExecutionProvider")

    @property
    def intra_op_threads(self) -> int:
        """ONNX intra-op thread count for this hardware/provider."""
        return self._intra_op_threads

    def apply_to_session(self, sess_options: _SessionOptions) -> None:
        """Set the ONNX intra/inter-op thread counts on *sess_options*."""
        sess_options.intra_op_num_threads = self._intra_op_threads
        # Inter-op stays 1: DES-027's narenas:1 means extra threads only contend.
        sess_options.inter_op_num_threads = 1

    def apply_env_limits(self) -> Self:
        """Cap rayon/OMP pools, logging the effective OMP and warning on divergence."""
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        os.environ.setdefault("OMP_NUM_THREADS", str(self._omp_threads))
        omp = os.environ["OMP_NUM_THREADS"]
        if omp != str(self._omp_threads):
            logger.warning(
                "OMP_NUM_THREADS preset to %s, not the cap %d; DES-032 "
                "oversubscription mitigation may be defeated",
                omp,
                self._omp_threads,
            )
        logger.info(
            "Thread config: intra_op=%d, inter_op=1, OMP=%s (ncpu=%d)",
            self._intra_op_threads,
            omp,
            self._ncpu,
        )
        return self
