"""Factory for OCR and embedding backends with thread-safe caching."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.types import EmbeddingBackend, OcrBackend

_ocr_cache: dict[str, OcrBackend] = {}
_embedding_cache: dict[str, EmbeddingBackend] = {}
_lock = threading.Lock()


def get_ocr_backend(settings: Settings) -> OcrBackend:
    """Return a cached local OCR backend instance."""
    key = "local"
    if key not in _ocr_cache:
        with _lock:
            if key not in _ocr_cache:
                from quarry.ingestion.ocr_local import (  # noqa: PLC0415
                    LocalOcrBackend,
                )

                _ocr_cache[key] = LocalOcrBackend(settings)
    return _ocr_cache[key]


def get_embedding_backend(settings: Settings) -> EmbeddingBackend:  # noqa: ARG001
    """Return a cached ONNX embedding backend instance."""
    key = "onnx"
    if key not in _embedding_cache:
        with _lock:
            if key not in _embedding_cache:
                from quarry.embeddings import (  # noqa: PLC0415
                    OnnxEmbeddingBackend,
                )

                _embedding_cache[key] = OnnxEmbeddingBackend()
    return _embedding_cache[key]


def clear_caches() -> None:
    """Clear all backend caches. For test isolation only."""
    _ocr_cache.clear()
    _embedding_cache.clear()
