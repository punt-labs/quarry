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


def _raise_cleanup_over_root(root_message: str, cleanup_message: str) -> OSError:
    """Return an error raised while handling another, chained as its cause."""
    try:
        try:
            raise ValueError(root_message)
        except ValueError as root:
            raise OSError(errno.EIO, cleanup_message) from root
    except OSError as exc:
        return exc
    raise AssertionError  # pragma: no cover - the raise above always lands


def test_the_root_failure_survives_a_cleanup_that_raised_another() -> None:
    exc = _raise_cleanup_over_root("root cause LEAK", "cleanup detail")

    chained = RedactedFailure.of(exc)
    rendered = "".join(
        traceback.format_exception(type(chained), chained, chained.__traceback__)
    )

    assert "ValueError" in rendered  # the root, as a stand-in of its own
    assert "OSError [EIO]" in rendered
    assert "LEAK" not in rendered
    assert "cleanup detail" not in rendered


def test_a_context_without_a_cause_is_chained_too() -> None:
    try:
        try:
            raise KeyError("first")
        except KeyError:
            raise RuntimeError("second") from None
    except RuntimeError as exc:
        # ``from None`` suppresses the display, but the context is still there
        assert exc.__context__ is not None
        stand_in = RedactedFailure.of(exc)

    assert stand_in.__cause__ is not None
    assert type(stand_in.__cause__).__name__ == "ReportedError"
    assert str(stand_in.__cause__) == "KeyError"


def test_a_long_chain_is_cut_at_the_limit() -> None:
    exc: BaseException = ValueError("0")
    for depth in range(1, RedactedFailure.MAX_CHAIN + 5):
        new = ValueError(str(depth))
        new.__cause__ = exc
        exc = new

    stand_in: BaseException | None = RedactedFailure.of(exc)
    length = 0
    while stand_in is not None:
        length += 1
        stand_in = stand_in.__cause__

    assert length == RedactedFailure.MAX_CHAIN + 1
