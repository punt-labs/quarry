"""Tests for quarry.failure_reason: what a failure tells a client."""

from __future__ import annotations

from quarry.content_guard import UnindexableContentError
from quarry.failure_reason import FailureReason


def test_a_refusal_keeps_its_stable_reason() -> None:
    exc = UnindexableContentError("binary content rejected: pdf header")
    assert FailureReason.of(exc) == "binary content rejected: pdf header"


def test_any_other_exception_is_its_class_only() -> None:
    exc = OSError("[Errno 13] Permission denied: '/home/me/secret/key.pem'")
    assert FailureReason.of(exc) == "internal error: OSError"
