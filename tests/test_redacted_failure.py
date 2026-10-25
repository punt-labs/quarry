"""Tests for quarry.redacted_failure: a failure's class and frames, no message."""

from __future__ import annotations

import errno
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


def test_an_os_error_names_its_errno_and_never_its_path() -> None:
    try:
        raise OSError(errno.ENOSPC, "no space", "/home/me/secret.md")
    except OSError as exc:
        stand_in = RedactedFailure.of(exc)

    assert str(stand_in) == "OSError [ENOSPC]"


def test_an_os_error_without_an_errno_is_named_by_class_alone() -> None:
    try:
        raise OSError("bare")
    except OSError as exc:
        assert str(RedactedFailure.of(exc)) == "OSError"
