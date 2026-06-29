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
                from quarry.ingestion.ocr_local import LocalOcrBackend  # noqa: PLC0415

                _ocr_cache[key] = LocalOcrBackend(settings)
    return _ocr_cache[key]


def new_embedding_backend() -> EmbeddingBackend:
    """Return a fresh, uncached ONNX backend with its own session (DES-032)."""
    from quarry.embeddings import OnnxEmbeddingBackend  # noqa: PLC0415

    return OnnxEmbeddingBackend()


def get_embedding_backend(settings: Settings) -> EmbeddingBackend:  # noqa: ARG001
    """Return a cached ONNX embedding backend instance."""
    key = "onnx"
    if key not in _embedding_cache:
        with _lock:
            if key not in _embedding_cache:
                _embedding_cache[key] = new_embedding_backend()
    return _embedding_cache[key]


def clear_caches() -> None:
    """Clear all backend caches. For test isolation only."""
    _ocr_cache.clear()
    _embedding_cache.clear()
