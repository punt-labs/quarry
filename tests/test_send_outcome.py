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
