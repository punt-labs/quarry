"""Thread-pool limits for ONNX inference under concurrent quarry processes.

Several quarry processes run at once — the serve daemon, a background ingest
worker, and short-lived CLI invocations.  Each one, left to defaults, spins up
ncpu rayon (tokenizer) threads plus ncpu ONNX threads.  Three processes on 8
cores reach 3x(8+8)=48 runnable threads and a load average near 148, which
starves the latency-sensitive query path.

``ThreadConfig`` derives a conservative thread budget from the hardware and the
selected provider, applies the rayon/OMP caps via environment variables, and
hands back the ONNX intra-op count.  A GPU provider offloads the GEMMs to CUDA,
so one CPU thread suffices to feed the pipeline; a CPU provider benefits from up
to two intra-op threads, beyond which jemalloc arena contention (DES-027 pins
narenas:1) erases the gains.  See DES-032.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, Self

logger = logging.getLogger(__name__)

_MAX_CPU_THREADS = 2


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
        self._ncpu = os.cpu_count() or 4
        self._omp_threads = min(_MAX_CPU_THREADS, self._ncpu)
        # GPU does the GEMMs on-device; one CPU thread feeds it.  CPU caps at
        # two — more contends on DES-027's single jemalloc arena.
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
        """Set the ONNX intra/inter-op thread counts on *sess_options*.

        Inter-op stays 1: DES-027 pins jemalloc narenas:1, so extra session
        threads contend on a single arena rather than parallelising.
        """
        sess_options.intra_op_num_threads = self._intra_op_threads
        sess_options.inter_op_num_threads = 1

    def apply_env_limits(self) -> Self:
        """Cap rayon (tokenizer) and OMP pools before any library creates them.

        Uses ``setdefault`` so an explicit operator override is preserved.
        Returns ``self`` so callers can build and apply in one expression.
        """
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        os.environ.setdefault("OMP_NUM_THREADS", str(self._omp_threads))
        logger.info(
            "Thread config: intra_op=%d, inter_op=1, OMP=%d (ncpu=%d)",
            self._intra_op_threads,
            self._omp_threads,
            self._ncpu,
        )
        return self
