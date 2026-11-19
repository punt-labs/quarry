"""Probe whether local OCR can run and explain the fix when it cannot."""

from __future__ import annotations

import importlib
import sys
from typing import Self, final

from quarry.opencv_headless import HeadlessOpenCv

__all__ = ["OcrAvailability", "OcrUnavailableError"]

# The remediation command lives on HeadlessOpenCv so doctor and this runtime
# warning quote the SAME working command (uv/--python/--no-deps when uv is
# present); a bare `pip install` would misdirect the uv-tool installs this
# guard exists to cover. `{fix}` is filled from HeadlessOpenCv.remediation().
_UNAVAILABLE_TEMPLATE = (
    "local OCR unavailable: the headless OpenCV isn't loadable on this machine "
    "(scanned-image OCR is off; everything else works) — run `{fix}` to enable it"
)
# Cap the underlying-error detail so a verbose loader message (a long dlopen
# search path) does not swamp the actionable remediation in logs.
_CAUSE_MAX = 160


class OcrUnavailableError(RuntimeError):
    """Raised when local OCR is invoked but OpenCV (cv2) will not load."""


@final
class OcrAvailability:
    """Whether local OCR can run, and the actionable reason when it cannot.

    Local OCR is an optional capability: rapidocr imports cv2, and the desktop
    OpenCV build it hard-requires links GUI system libraries a headless machine
    lacks. Probing cv2 once lets callers degrade cleanly — a clear message, OCR
    off — instead of crashing on a raw ``libGL``/``libxcb`` ``ImportError``.
    """

    __slots__ = ("_available", "_cause", "_reason")

    _available: bool
    _reason: str
    # None is the documented contract for the available state: there is no
    # underlying failure to preserve when cv2 loaded cleanly.
    _cause: BaseException | None

    def __new__(
        cls, *, available: bool, reason: str, cause: BaseException | None = None
    ) -> Self:
        """Freeze an OCR probe outcome and its optional underlying failure."""
        self = super().__new__(cls)
        self._available = available
        self._reason = reason
        self._cause = cause
        return self

    @classmethod
    def probe(cls) -> Self:
        """Return current OCR availability by attempting to load cv2.

        A boundary probe of an optional native dependency: any failure to load
        cv2 means OCR is off. A headless GUI-linked build raises ``ImportError``
        (``libGL``/``libxcb``), but a broken cv2 can also raise ``AttributeError``
        (mismatched typing stubs) or other errors on import, so every load
        failure is treated as unavailable rather than only ``ImportError``.
        """
        try:
            # Execute the cv2 module (its bootstrap loads the native library that
            # fails on a GUI-linked build) — rapidocr's transitive dependency.
            importlib.import_module("cv2")
        except Exception as exc:  # noqa: BLE001  # any cv2 load failure = OCR unavailable
            return cls(available=False, reason=cls._unavailable_reason(exc), cause=exc)
        return cls(available=True, reason="")

    @classmethod
    def _unavailable_reason(cls, exc: Exception) -> str:
        """Return the fix message, appending a short form of the cv2 failure.

        The underlying error distinguishes a headless box (``libGL`` missing)
        from a corrupt install or ABI mismatch, so the operator keeps the real
        cause instead of a generic "not loadable".
        """
        fix = HeadlessOpenCv(sys.executable).remediation()
        message = _UNAVAILABLE_TEMPLATE.format(fix=fix)
        detail = f"{type(exc).__name__}: {exc}".strip()
        if len(detail) > _CAUSE_MAX:
            detail = f"{detail[:_CAUSE_MAX]}…"
        return f"{message} [underlying error: {detail}]"

    @property
    def is_available(self) -> bool:
        """Return whether local OCR can run on this machine."""
        return self._available

    @property
    def reason(self) -> str:
        """Return the actionable unavailability message, or ``""`` when available."""
        return self._reason

    def require(self) -> None:
        """Raise :class:`OcrUnavailableError` when OCR is unavailable.

        Chain the captured cv2 failure so the real cause survives in the
        traceback for troubleshooting.
        """
        if not self._available:
            raise OcrUnavailableError(self._reason) from self._cause
