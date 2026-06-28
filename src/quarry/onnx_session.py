"""ONNX inference-session creation with GPU-to-CPU graceful degradation.

Building a CUDA session can fail at runtime for reasons that only surface when
onnxruntime actually tries to initialise the provider — a missing cuDNN, a
driver/cuBLAS mismatch, or an out-of-memory GPU.  When that happens and the
operator has not pinned CUDA, the right behaviour is to fall back to the CPU +
int8 model rather than crash the daemon.  ``OnnxSessionBuilder`` owns ONNX
session construction — options, thread limits, and that fallback policy — so
the embedding backend's constructor stays a thin orchestrator and onnxruntime
is imported in exactly one place.  See DES-016, DES-032.
"""

from __future__ import annotations

import logging
from types import ModuleType
from typing import TYPE_CHECKING, Self

from quarry.ingestion.provider import PROVIDER_MODEL_MAP

if TYPE_CHECKING:
    from collections.abc import Callable

    import onnxruntime as ort

    from quarry.ingestion.provider import ProviderSelection
    from quarry.thread_config import ThreadConfig

logger = logging.getLogger(__name__)

# Substrings marking an ONNX session error as CUDA-related (case-folded).
_CUDA_ERROR_MARKERS: tuple[str, ...] = (
    "cuda",
    "cublas",
    "cudnn",
    "gpu",
    "cudaexecutionprovider",
    "failed to create cuda",
)

_CPU_PROVIDER = "CPUExecutionProvider"


class OnnxSessionBuilder:
    """Builds an ONNX session for a provider, degrading GPU→CPU on CUDA failure.

    Configured once with the chosen provider, the thread budget, whether CUDA
    is pinned, and a callback that resolves the CPU int8 model path.  ``build``
    constructs the session options, applies thread limits, and creates the
    session — falling back to CPU when a CUDA session fails for a recoverable
    reason.
    """

    _selection: ProviderSelection
    _threads: ThreadConfig
    _force_cuda: bool
    _load_cpu_model: Callable[[str], str]

    def __new__(
        cls,
        selection: ProviderSelection,
        threads: ThreadConfig,
        *,
        force_cuda: bool,
        load_cpu_model: Callable[[str], str],
    ) -> Self:
        self = super().__new__(cls)
        self._selection = selection
        self._threads = threads
        self._force_cuda = force_cuda
        self._load_cpu_model = load_cpu_model
        return self

    @staticmethod
    def _is_cuda_error(exc: Exception) -> bool:
        """Return whether *exc* looks like a CUDA provider initialisation failure."""
        text = str(exc).lower()
        return any(marker in text for marker in _CUDA_ERROR_MARKERS)

    def _session_options(self, ort_module: ModuleType) -> ort.SessionOptions:
        """Build session options with full graph optimization and thread limits."""
        options = ort_module.SessionOptions()
        options.graph_optimization_level = (
            ort_module.GraphOptimizationLevel.ORT_ENABLE_ALL
        )
        self._threads.apply_to_session(options)
        return options

    def build(self, model_path: str) -> ort.InferenceSession:
        """Build the session, falling back to CPU+int8 on recoverable CUDA failure.

        Raises if CUDA was pinned, the error is not CUDA-related, or the CPU
        fallback also fails.  onnxruntime is imported here — the one heavy-import
        site for the whole embedding stack.
        """
        import onnxruntime as ort_module  # noqa: PLC0415

        options = self._session_options(ort_module)
        provider = self._selection.provider
        try:
            session = ort_module.InferenceSession(
                model_path, sess_options=options, providers=[provider]
            )
        except Exception as cuda_exc:
            eligible = (
                provider == "CUDAExecutionProvider"
                and not self._force_cuda
                and self._is_cuda_error(cuda_exc)
            )
            if not eligible:
                raise
            return self._build_cpu_fallback(ort_module, options, cuda_exc)
        logger.info(
            "ONNX model loaded: provider=%s, model=%s",
            provider,
            self._selection.model_file,
        )
        return session

    def _build_cpu_fallback(
        self, ort_module: ModuleType, options: ort.SessionOptions, cuda_exc: Exception
    ) -> ort.InferenceSession:
        """Build a CPU+int8 session after a CUDA session failure, or raise."""
        logger.warning("CUDA session failed, falling back to CPU + int8", exc_info=True)
        cpu_model_file = PROVIDER_MODEL_MAP[_CPU_PROVIDER]
        model_path = self._load_cpu_model(cpu_model_file)
        try:
            session = ort_module.InferenceSession(
                model_path, sess_options=options, providers=[_CPU_PROVIDER]
            )
        except Exception as cpu_exc:
            msg = (
                "CPU fallback also failed after CUDA session error. "
                f"CUDA error: {cuda_exc}"
            )
            raise RuntimeError(msg) from cpu_exc
        logger.info(
            "ONNX model loaded: provider=%s, model=%s", _CPU_PROVIDER, cpu_model_file
        )
        return session
