"""Tests for quarry.redacted_failure: a failure's class and frames, no message."""

from __future__ import annotations

import traceback

from quarry.redacted_failure import RedactedFailure


def test_the_stand_in_carries_the_class_and_the_frames_and_no_message() -> None:
    try:
        msg = "boom https://u:LEAK@h.test/p"
        raise KeyError(msg)
    except KeyError as exc:
        stand_in = RedactedFailure.of(exc)
    rendered = "".join(
        traceback.format_exception(type(stand_in), stand_in, stand_in.__traceback__)
    )
    assert "KeyError" in rendered
    assert "test_redacted_failure" in rendered
    assert "LEAK" not in rendered
    assert "boom" not in rendered
