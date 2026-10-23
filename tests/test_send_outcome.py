"""Tests for quarry.send_outcome: the one mapping from a send to its trace words."""

from __future__ import annotations

import pytest

from quarry.send_outcome import SendOutcome


@pytest.mark.parametrize(
    ("outcome", "label"),
    [
        (SendOutcome.unreachable("down"), "daemon-unreachable"),
        (SendOutcome.rejected(400, "no"), "daemon-refused"),
        (SendOutcome.rejected(422, "no"), "daemon-refused"),
        (SendOutcome.rejected(500, "boom"), "daemon-error"),
        (SendOutcome.error("misconfigured"), "daemon-error"),
    ],
)
def test_each_failure_has_its_label(outcome: SendOutcome, label: str) -> None:
    assert not outcome
    assert outcome.label == label


def test_a_step_after_a_send_is_named_by_how_the_send_went() -> None:
    assert SendOutcome(sent=True).named("distilled", "distill") == "distilled"
    assert SendOutcome.rejected(400, "no").named("distilled", "distill") == (
        "distill-daemon-refused"
    )


def test_only_an_unreachable_daemon_is_told_to_backfill() -> None:
    assert "backfill-sessions" in SendOutcome.advice("daemon-unreachable")
    for label in ("daemon-refused", "daemon-error"):
        assert "backfill" not in SendOutcome.advice(label)


def test_a_refusal_and_a_fault_are_told_where_to_look() -> None:
    assert "the daemon refused the capture; see quarry.log" in SendOutcome.advice(
        "daemon-refused"
    )
    assert "the daemon failed; see quarry.log or quarry doctor" in SendOutcome.advice(
        "daemon-error"
    )
