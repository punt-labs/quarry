"""Tests for quarry.failure_reason: what a failure tells a client."""

from __future__ import annotations

from quarry.content_guard import UnindexableContentError
from quarry.db.row_budget import RowBudgetError
from quarry.failure_reason import FailureReason
from quarry.refusal import RefusalError, RegistrationRefusedError


def test_a_refusal_keeps_its_stable_reason() -> None:
    exc = UnindexableContentError("binary content rejected: pdf header")
    assert FailureReason.of(exc) == "binary content rejected: pdf header"


def test_any_other_exception_is_its_class_only() -> None:
    exc = OSError("[Errno 13] Permission denied: '/home/me/secret/key.pem'")
    assert FailureReason.of(exc) == "internal error: OSError"


def test_a_row_budget_violation_keeps_its_reason() -> None:
    exc = RowBudgetError("sheet exceeds the page budget")
    assert FailureReason.of(exc) == "sheet exceeds the page budget"


def test_a_registration_refusal_keeps_its_reason() -> None:
    exc = RegistrationRefusedError("collection name already in use: 'docs'")
    assert FailureReason.of(exc) == "collection name already in use: 'docs'"


def test_a_stray_value_error_fails_closed() -> None:
    exc = ValueError("bad value from /home/me/secret.txt")
    assert FailureReason.of(exc) == "internal error: ValueError"


def test_line_joins_the_name_and_the_reason() -> None:
    exc = OSError("[Errno 2] No such file: '/home/me/x'")
    assert FailureReason.line("doc.md", exc) == "doc.md: internal error: OSError"


def test_every_expected_refusal_is_a_refusal() -> None:
    assert issubclass(UnindexableContentError, RefusalError)
    assert issubclass(RowBudgetError, RefusalError)
    assert issubclass(RegistrationRefusedError, RefusalError)
    assert not issubclass(ValueError, RefusalError)


def test_frames_name_the_code_and_not_the_message() -> None:
    try:
        msg = "boom https://u:SECRET@h.test/p"
        raise RuntimeError(msg)
    except RuntimeError as exc:
        frames = FailureReason.frames(exc)
    assert "test_failure_reason" in frames
    assert "SECRET" not in frames
    assert "boom" not in frames
