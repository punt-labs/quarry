"""Determinism pinning and the provenance stamp a committed baseline carries.

A baseline number is only a regression signal against another run on the *same*
profile, so every committed baseline is stamped with the ONNX Runtime version,
the pinned model revision, the CPU arch, numpy's version, and the effective ORT
intra-op thread count. ``Determinism.apply`` pins the thread/BLAS environment
before the embedding session is built.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from typing import Self

from quarry.config import ONNX_MODEL_REPO, ONNX_MODEL_REVISION

# Single-thread every pool that can reorder a float reduction. Forced (not
# setdefault) so determinism wins over any ambient cap; ThreadConfig later
# reads OMP via setdefault, so this value survives.
_THREAD_ENV = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)


class Determinism:
    """Pin the process so an embedding run is reproducible on one machine."""

    __slots__ = ()

    @staticmethod
    def apply() -> None:
        """Force single-threaded BLAS/OMP and disable tokenizer parallelism.

        Call before the embedding backend is constructed. It cannot lower the
        ORT intra-op pool below ThreadConfig's CPU floor (that lives behind the
        frozen embedding seam), so the effective intra-op count is *stamped*
        into provenance rather than forced here.
        """
        for var in _THREAD_ENV:
            os.environ[var] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"

    @staticmethod
    def effective_intra_op_threads() -> int:
        """Return the ORT intra-op thread count the embedding session will use."""
        from quarry.ingestion.provider import ProviderSelection  # noqa: PLC0415
        from quarry.thread_config import ThreadConfig  # noqa: PLC0415

        provider = ProviderSelection.from_environment().provider
        return ThreadConfig.for_provider(provider).intra_op_threads


@dataclass(frozen=True, slots=True)
class Provenance:
    """The reproducibility profile stamped onto a committed baseline."""

    ort_version: str
    model_repo: str
    model_revision: str
    cpu_arch: str
    numpy_version: str
    intra_op_threads: int

    @classmethod
    def capture(cls) -> Self:
        """Read the current runtime's reproducibility profile."""
        import numpy as np  # noqa: PLC0415
        import onnxruntime as ort  # noqa: PLC0415

        return cls(
            ort_version=str(ort.__version__),
            model_repo=ONNX_MODEL_REPO,
            model_revision=ONNX_MODEL_REVISION,
            cpu_arch=platform.machine(),
            numpy_version=str(np.__version__),
            intra_op_threads=Determinism.effective_intra_op_threads(),
        )

    def to_dict(self) -> dict[str, str | int]:
        """Return the JSON-serializable stamp (a provenance serialization boundary)."""
        return {
            "ort_version": self.ort_version,
            "model_repo": self.model_repo,
            "model_revision": self.model_revision,
            "cpu_arch": self.cpu_arch,
            "numpy_version": self.numpy_version,
            "intra_op_threads": self.intra_op_threads,
        }
