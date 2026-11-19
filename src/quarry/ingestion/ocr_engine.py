"""Provide a process-wide cached RapidOCR engine behind the headless-cv2 guard."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, ClassVar, Protocol, cast, final

from quarry.ingestion.ocr_availability import OcrAvailability, OcrUnavailableError

if TYPE_CHECKING:
    from PIL import Image

logger = logging.getLogger(__name__)

# Building the engine fails one of two ways on a machine that cannot OCR: cv2
# won't load on a headless box (``OcrUnavailableError`` from the probe) or
# rapidocr isn't installed (``ImportError`` on its import). Both mean "OCR is
# off" — callers degrade, don't crash — and both are memoized by the cache so
# the probe and the import each run at most once per process.
OCR_UNAVAILABLE: tuple[type[Exception], ...] = (OcrUnavailableError, ImportError)


class OcrResult(Protocol):
    """Structural type for RapidOCR v3 output."""

    @property
    def txts(self) -> tuple[str, ...] | None:
        """Expose recognized text lines, or None when OCR finds no text."""
        ...


class OcrEngineProtocol(Protocol):
    """Structural type for a callable RapidOCR engine."""

    def __call__(self, img: Image.Image) -> OcrResult:
        """Recognize text from a Pillow image using the configured local engine."""
        ...


@final
class OcrEngine:
    """Lazily create and cache one RapidOCR engine for the whole process.

    The engine is expensive to construct (loads ONNX models), so it is built
    once under a lock and reused. :meth:`get` runs the headless-OpenCV guard
    first, turning a raw ``libGL``/``libxcb`` ``ImportError`` into a clear
    :class:`OcrUnavailableError` before RapidOCR imports the GUI-linked ``cv2``.
    """

    __slots__ = ()

    _instance: ClassVar[OcrEngineProtocol | None] = None
    # None is the "engine available or not yet probed" state; a non-None value
    # is the cached unavailability (headless cv2 or missing rapidocr).
    _unavailable: ClassVar[Exception | None] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def get(cls) -> OcrEngineProtocol:
        """Return the cached engine, building it on first use (thread-safe).

        The build runs at most once per process. When OCR is unavailable — a
        headless box whose ``cv2`` won't load, or a missing ``rapidocr`` — the
        raised exception is cached and re-raised on every later call, so
        repeated ``ocr_document``/``ocr_image_bytes`` calls never re-probe cv2
        or re-attempt the ``rapidocr`` import.
        """
        cached = cls._cached()
        if cached is not None:
            return cached
        with cls._lock:
            cached = cls._cached()
            if cached is not None:
                return cached
            return cls._build()

    @classmethod
    def _cached(cls) -> OcrEngineProtocol | None:
        """Return the cached engine, re-raise the cached error, or None if unprobed.

        None is the "not yet probed" state of the tri-state cache (engine built /
        unavailable / unknown); the caller then probes under the lock.
        """
        if cls._instance is not None:
            return cls._instance
        if cls._unavailable is not None:
            raise cls._unavailable
        return None

    @classmethod
    def _build(cls) -> OcrEngineProtocol:
        """Probe cv2 and import rapidocr, caching any unavailability (called locked)."""
        try:
            OcrAvailability.probe().require()
            from rapidocr import RapidOCR  # noqa: PLC0415
        except OCR_UNAVAILABLE as exc:
            # Cache ANY unavailability — headless cv2 (OcrUnavailableError, whose
            # __cause__ is the real load failure) or missing rapidocr
            # (ImportError) — so later calls re-raise it without re-probing or
            # re-importing.
            cls._unavailable = exc
            raise
        engine = cast("OcrEngineProtocol", RapidOCR())
        cls._instance = engine
        logger.info("RapidOCR engine initialized")
        return engine

    @classmethod
    def reset(cls) -> None:
        """Drop the cached engine and probe outcome so the next :meth:`get` rebuilds."""
        cls._instance = None
        cls._unavailable = None
