"""Factory for OCR and embedding backends with thread-safe caching."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.types import EmbeddingBackend, OcrBackend

_ocr_cache: dict[str, OcrBackend] = {}
_embedding_cache: dict[tuple[str, str], EmbeddingBackend] = {}
_lock = threading.Lock()


def get_ocr_backend(settings: Settings) -> OcrBackend:
    """Return a cached OCR backend instance for the configured backend.

    Raises:
        ValueError: If ``settings.ocr_backend`` is not a known backend.
    """
    key = settings.ocr_backend
    if key not in _ocr_cache:
        with _lock:
            if key not in _ocr_cache:
                match key:
                    case "local":
                        from quarry.ocr_local import (  # noqa: PLC0415
                            LocalOcrBackend,
                        )

                        _ocr_cache[key] = LocalOcrBackend(settings)
                    case "textract":
                        from quarry.ocr_client import (  # noqa: PLC0415
                            TextractOcrBackend,
                        )

                        _ocr_cache[key] = TextractOcrBackend(settings)
                    case _:
                        available = "local, textract"
                        msg = f"Unknown OCR backend: '{key}'. Available: {available}"
                        raise ValueError(msg)
    return _ocr_cache[key]


def get_embedding_backend(settings: Settings) -> EmbeddingBackend:
    """Return a cached embedding backend instance for the configured backend.

    Raises:
        ValueError: If ``settings.embedding_backend`` is not a known backend.
    """
    key = (settings.embedding_backend, settings.embedding_model)
    if key not in _embedding_cache:
        with _lock:
            if key not in _embedding_cache:
                match settings.embedding_backend:
                    case "onnx":
                        from quarry.embeddings import (  # noqa: PLC0415
                            OnnxEmbeddingBackend,
                        )

                        _embedding_cache[key] = OnnxEmbeddingBackend()
                    case "sagemaker":
                        from quarry.embeddings_sagemaker import (  # noqa: PLC0415
                            SageMakerEmbeddingBackend,
                        )

                        _embedding_cache[key] = SageMakerEmbeddingBackend(settings)
                    case _:
                        available = "onnx, sagemaker"
                        msg = (
                            f"Unknown embedding backend: "
                            f"'{settings.embedding_backend}'. "
                            f"Available: {available}"
                        )
                        raise ValueError(msg)
    return _embedding_cache[key]


def clear_caches() -> None:
    """Clear all backend caches. For test isolation only."""
    _ocr_cache.clear()
    _embedding_cache.clear()
