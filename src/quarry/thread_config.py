"""Thread-pool limits for ONNX inference and LanceDB compaction.

Concurrent quarry processes (serve daemon, ingest worker, CLI) each default to
ncpu rayon + ncpu ONNX threads — three on 8 cores reach load ~148 and starve the
query path.  ``ThreadConfig`` caps the budget per hardware/provider: GPU offloads
GEMMs to CUDA (1 feeder thread), CPU caps at 2 (DES-027 arena).  See DES-032.

LanceDB is the second, larger offender.  Its Rust core sizes its compute and
tokio pools to ``num_cpus`` and floods every core during ``optimize()`` /
``create_fts_index`` (native FTS, not Tantivy) — the daemon's watch loop fires
these on ordinary file churn, spiking to 300-400% CPU.  ``LANCE_CPU_THREADS``
governs those pools: their sizes track the cap, not the core count (measured
lance-cpu/tokio 6/33 uncapped, 3/14 at cap 2 on 8 cores), so a small cap bounds
lance no matter how many cores are present.  The cap is applied fail-closed — an
inherited value is honored only inside ``[floor, cap]``; a higher one is clamped
down and a below-floor one raised up (see ``_cap_env``), so neither a stale
``LANCE_CPU_THREADS=32`` export nor a ``=1`` can escape the bound.  The LANCE
floor is 2 because 1 stalls lance's runtime; OMP floors at 1 (it does not stall).
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, Self

logger = logging.getLogger(__name__)

_MAX_CPU_THREADS = 2
# LanceDB's compute pool is a FIXED 2, not min(2, ncpu): LANCE_CPU_THREADS=1
# stalls lance's runtime (observed 0% CPU / no progress on a compaction loop), so
# a one-core host must still get 2 — deriving min(2, ncpu) there would hand it 1
# and deadlock. 2 is both floor and ceiling, independent of core count.
_LANCE_THREADS = 2
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
    _lance_threads: int

    def __new__(cls, *, is_gpu: bool) -> Self:
        """Derive CPU and inference thread budgets from hardware and GPU selection."""
        self = super().__new__(cls)
        if (detected := os.cpu_count()) is None:
            logger.warning("os.cpu_count() returned None; assuming %d CPUs", _NCPU_NONE)
        self._ncpu = detected or _NCPU_NONE
        self._omp_threads = min(_MAX_CPU_THREADS, self._ncpu)
        # GPU does the GEMMs (1 feeder thread); CPU caps at 2 (DES-027 arena).
        self._intra_op_threads = 1 if is_gpu else min(_MAX_CPU_THREADS, self._ncpu)
        # LanceDB's compute pool is a fixed 2, provider- AND core-count-independent
        # (the daemon holds the connection whether or not the GPU embeds; 1 stalls
        # lance, so a one-core host still gets 2).
        self._lance_threads = _LANCE_THREADS
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
        """Clamp the rayon/OMP and LanceDB thread pools for this whole process.

        Idempotent, so a redundant call from the embedding backend after the
        daemon boot call is a no-op.  Must run before the first ``lancedb.connect``
        — lance reads ``LANCE_CPU_THREADS`` once when it builds its compute
        runtime, so a later set is ignored.
        """
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        # OMP floors at 1 — it does not stall at a single thread.
        omp = self._cap_env("OMP_NUM_THREADS", self._omp_threads, floor=1)
        # LanceDB's tokio compute pool (compaction, FTS rebuild) — the ceiling
        # that stops the daemon seizing every core on watch-loop churn.  It ALSO
        # floors at 2: LANCE_CPU_THREADS=1 stalls lance's runtime, so an inherited
        # or operator "1" must be raised, not honored as a "tighter" bound.
        lance_cpu = self._cap_env(
            "LANCE_CPU_THREADS", self._lance_threads, floor=self._lance_threads
        )
        self._cap_env(
            "LANCE_IO_THREADS", self._lance_threads, floor=self._lance_threads
        )
        # Log the EFFECTIVE (post-clamp) values, not the intended ones — an
        # operator preset below the cap is honored, so the two can differ.
        logger.info(
            "Thread config: intra_op=%d, inter_op=1, OMP=%s, LANCE_CPU=%s (ncpu=%d)",
            self._intra_op_threads,
            omp,
            lance_cpu,
            self._ncpu,
        )
        return self

    @staticmethod
    def _cap_env(name: str, cap: int, *, floor: int) -> str:
        """Clamp ``name`` into ``[floor, cap]`` fail-closed; return the effective value.

        ``setdefault`` yields upward: a stale export or dev-shell ``OMP_NUM_THREADS=32``
        would survive as-is and the daemon would run at 32, defeating the ceiling.
        Clamp instead — honor an in-range operator value (a legitimate tightening),
        force a higher one down to ``cap`` and raise a lower one up to ``floor``,
        warning on either adjustment.  ``floor`` matters for the LANCE vars: a value
        of 1 STALLS lance's runtime, so ``floor=2`` there turns an inherited "1"
        into a warning-and-raise, never a silently-hung daemon.  A non-numeric
        preset is replaced by ``cap`` (fail closed).
        """
        preset = os.environ.get(name)
        if preset is not None and preset.isdigit():
            value = int(preset)
            if floor <= value <= cap:
                return preset  # in [floor, cap] — a legitimate operator choice
            effective = min(max(value, floor), cap)  # clamp toward the nearer bound
        else:
            effective = cap  # absent or non-numeric — fail closed to the intended cap
        os.environ[name] = str(effective)
        if preset is not None and preset != str(effective):
            logger.warning(
                "%s preset to %r not honored (outside [%d, %d] or invalid); using %d",
                name,
                preset,
                floor,
                cap,
                effective,
            )
        return str(effective)
