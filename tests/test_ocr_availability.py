"""Tests for the local-OCR availability probe."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest

from quarry.ingestion.ocr_availability import OcrAvailability, OcrUnavailableError
from quarry.opencv_headless import HeadlessOpenCv


def _break_cv2_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make loading cv2 raise ImportError, as on a headless GUI-linked build.

    The probe calls ``importlib.import_module("cv2")``, so that is what the test
    breaks (not ``builtins.__import__``, which import_module bypasses).
    """
    real = importlib.import_module

    def fake(name: str, package: str | None = None) -> ModuleType:
        if name == "cv2":
            msg = "libGL.so.1: cannot open shared object file"
            raise ImportError(msg)
        return real(name, package)

    monkeypatch.setattr(importlib, "import_module", fake)


def test_available_when_cv2_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a cleanly-loadable cv2 (the CI/dev cv2 build may itself be broken).
    real = importlib.import_module

    def fake(name: str, package: str | None = None) -> ModuleType:
        if name == "cv2":
            return ModuleType("cv2")
        return real(name, package)

    monkeypatch.setattr(importlib, "import_module", fake)
    availability = OcrAvailability.probe()
    assert availability.is_available is True
    assert availability.reason == ""
    availability.require()  # must not raise


def test_unavailable_when_cv2_import_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _break_cv2_import(monkeypatch)
    availability = OcrAvailability.probe()
    assert availability.is_available is False
    assert availability.reason  # non-empty, actionable
    assert "opencv-python-headless" in availability.reason
    assert "--force-reinstall" in availability.reason


def test_reason_quotes_headless_remediation_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The runtime OCR-unavailable message must quote HeadlessOpenCv's actual
    # command (uv/--python/--no-deps when uv is present), not a bare `pip
    # install` that would misdirect the uv-tool installs this guard covers.
    _break_cv2_import(monkeypatch)
    reason = OcrAvailability.probe().reason
    assert HeadlessOpenCv(sys.executable).remediation() in reason
    assert "--no-deps" in reason  # a HeadlessOpenCv trait the bare hint lacked


def test_reason_includes_underlying_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real cv2 failure must survive in the message so a corrupt install or
    # ABI mismatch is distinguishable from a plain headless box.
    _break_cv2_import(monkeypatch)
    reason = OcrAvailability.probe().reason
    assert "underlying error" in reason
    assert "ImportError" in reason
    assert "libGL.so.1" in reason


def test_require_raises_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _break_cv2_import(monkeypatch)
    availability = OcrAvailability.probe()
    with pytest.raises(OcrUnavailableError, match="opencv-python-headless"):
        availability.require()


def test_require_chains_the_cv2_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Chain the cv2 exception so the operator
    # keeps the real cause in the traceback.
    _break_cv2_import(monkeypatch)
    availability = OcrAvailability.probe()
    with pytest.raises(OcrUnavailableError) as excinfo:
        availability.require()
    assert isinstance(excinfo.value.__cause__, ImportError)
    assert "libGL.so.1" in str(excinfo.value.__cause__)
