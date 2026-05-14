"""ONNX Runtime execution provider auto-detection."""

from __future__ import annotations

import functools
import logging
import os
from dataclasses import dataclass
from typing import Self

logger = logging.getLogger(__name__)

PROVIDER_MODEL_MAP: dict[str, str] = {
    "CUDAExecutionProvider": "onnx/model_fp16.onnx",
    "CPUExecutionProvider": "onnx/model_int8.onnx",
}


@dataclass(frozen=True)
class ProviderSelection:
    """Selected ONNX Runtime provider and corresponding model file."""

    provider: str  # e.g. "CUDAExecutionProvider"
    model_file: str  # e.g. "onnx/model_fp16.onnx" (HF repo-relative path)

    @classmethod
    def from_environment(cls) -> Self:
        """Detect the best ONNX Runtime execution provider.

        Reads ``QUARRY_PROVIDER`` env var.  Probes
        ``ort.get_available_providers()``.  Falls back to CPU when CUDA is
        unavailable.

        Returns a :class:`ProviderSelection` -- never raises for hardware
        issues (unless ``QUARRY_PROVIDER=cuda`` and CUDA is unavailable).

        Raises:
            ValueError: Unknown ``QUARRY_PROVIDER`` value.
            RuntimeError: ``QUARRY_PROVIDER=cuda`` but CUDA not available.
        """
        import onnxruntime as ort  # noqa: PLC0415

        value = os.environ.get("QUARRY_PROVIDER")
        force_cuda = False

        if value is not None:
            normalized = value.lower().strip()
            if normalized == "":
                pass  # treat empty as unset -- fall through to auto-detect
            elif normalized == "cpu":
                logger.info("Provider override: cpu (QUARRY_PROVIDER)")
                cpu = "CPUExecutionProvider"
                return cls(cpu, PROVIDER_MODEL_MAP[cpu])
            elif normalized == "cuda":
                force_cuda = True
            else:
                msg = (
                    f"Unknown QUARRY_PROVIDER value: {value!r}."
                    " Expected 'cpu', 'cuda', or unset."
                )
                raise ValueError(msg)

        available = ort.get_available_providers()
        if not available:
            logger.warning(
                "onnxruntime reported no available providers; "
                "this may indicate a broken installation. "
                "Attempting CPUExecutionProvider."
            )

        if "CUDAExecutionProvider" in available:
            cuda = "CUDAExecutionProvider"
            return cls(cuda, PROVIDER_MODEL_MAP[cuda])

        if force_cuda:
            msg = "QUARRY_PROVIDER=cuda but CUDAExecutionProvider not available"
            raise RuntimeError(msg)

        logger.info("Using CPUExecutionProvider + int8")
        cpu = "CPUExecutionProvider"
        return cls(cpu, PROVIDER_MODEL_MAP[cpu])

    def display(self) -> str:
        """Return a human-readable provider string for status output.

        Example: ``"CPUExecutionProvider (int8)"``
        or ``"CUDAExecutionProvider (fp16)"``.
        """
        variant = "fp16" if "fp16" in self.model_file else "int8"
        return f"{self.provider} ({variant})"

    @classmethod
    @functools.lru_cache(maxsize=1)
    def display_cached(cls) -> str:
        """Return a cached, error-safe display string.

        Cached per process -- provider detection runs once, not on every
        status call.  Returns ``"?"`` if provider detection fails.
        """
        try:
            return cls.from_environment().display()
        except Exception:  # noqa: BLE001
            logger.debug("Provider detection failed", exc_info=True)
            return "?"
