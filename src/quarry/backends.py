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
    """Return a cached OCR backend instance for the configured backend.

    Raises:
        ValueError: If ``settings.ocr_backend`` is not a known backend.
    """
    key = settings.ocr_backend
    if key not in _ocr_cache:
        with _lock:
            if key not in _ocr_cache:
                match key:
                    case "textract":
                        from quarry.ocr_client import (  # noqa: PLC0415
                            TextractOcrBackend,
                        )

                        _ocr_cache[key] = TextractOcrBackend(settings)
                    case _:
                        msg = f"Unknown OCR backend: '{key}'. Available: textract"
                        raise ValueError(msg)
    return _ocr_cache[key]


def get_embedding_backend(settings: Settings) -> EmbeddingBackend:
    """Return a cached embedding backend instance for the configured model."""
    key = settings.embedding_model
    if key not in _embedding_cache:
        with _lock:
            if key not in _embedding_cache:
                from quarry.embeddings import (  # noqa: PLC0415
                    SnowflakeEmbeddingBackend,
                )

                _embedding_cache[key] = SnowflakeEmbeddingBackend(settings)
    return _embedding_cache[key]


def clear_caches() -> None:
    """Clear all backend caches. For test isolation only."""
    _ocr_cache.clear()
    _embedding_cache.clear()
