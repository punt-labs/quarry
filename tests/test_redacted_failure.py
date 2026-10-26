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


def _chained(*names: str, via: str = "__context__") -> BaseException:
    """Return an exception whose *via* chain runs through *names*, newest first."""
    exc: BaseException | None = None
    for name in reversed(names):
        new = type(name, (Exception,), {})(name)
        if exc is not None:
            setattr(new, via, exc)
        exc = new
    assert exc is not None
    return exc


def _stand_ins(stand_in: BaseException) -> list[str]:
    """Return the messages along a stand-in's cause chain."""
    found: list[str] = []
    link: BaseException | None = stand_in
    while link is not None:
        found.append(str(link))
        link = link.__cause__
    return found


def test_an_implicit_context_is_chained_too() -> None:
    stand_in = RedactedFailure.of(_chained("Second", "First"))

    assert _stand_ins(stand_in) == ["Second", "First"]


def test_a_suppressed_context_is_not_chained() -> None:
    exc = _chained("Second", "First")
    exc.__suppress_context__ = True

    assert _stand_ins(RedactedFailure.of(exc)) == ["Second"]


def test_a_cause_wins_over_the_context_and_is_chained_even_if_suppressing() -> None:
    exc = _chained("Outer", "Cause", via="__cause__")
    exc.__context__ = _chained("Other")
    exc.__suppress_context__ = True

    assert _stand_ins(RedactedFailure.of(exc)) == ["Outer", "Cause"]


def test_a_long_chain_ends_with_a_stand_in_that_says_it_was_cut() -> None:
    names = [f"E{n}" for n in range(RedactedFailure.MAX_CHAIN + 4)]

    chain = _stand_ins(RedactedFailure.of(_chained(*names)))

    assert chain[:-1] == names[: RedactedFailure.MAX_CHAIN + 1]
    assert chain[-1] == f"chain truncated after {RedactedFailure.MAX_CHAIN + 1} links"


def test_a_cyclic_context_ends_with_the_truncation_stand_in() -> None:
    first = _chained("First")
    second = _chained("Second")
    first.__context__ = second
    second.__context__ = first

    chain = _stand_ins(RedactedFailure.of(first))

    assert chain == ["First", "Second", "chain truncated after 2 links"]


def test_an_exception_can_be_marked_logged_once_and_only_then() -> None:
    exc = ValueError("x")

    assert not RedactedFailure.is_logged(exc)
    RedactedFailure.mark_logged(exc)
    assert RedactedFailure.is_logged(exc)
    assert not RedactedFailure.is_logged(ValueError("x"))
