"""A test helper: a line through the redactor, which must settle."""

from __future__ import annotations

from quarry.log_redactor import LogRedactor


def redacted(text: str) -> str:
    """Return *text* redacted; fail the test if the passes do not settle."""
    settled = LogRedactor.settle(text)
    assert settled is not None, "redaction did not settle"
    return settled
