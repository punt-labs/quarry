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


_SAVED = {"archived": True, "written": True}


def test_only_an_unreachable_daemon_is_told_to_backfill() -> None:
    assert "backfill-sessions" in SendOutcome.advice("daemon-unreachable", **_SAVED)
    for label in ("daemon-refused", "daemon-error"):
        assert "backfill" not in SendOutcome.advice(label, **_SAVED)


def test_a_refusal_and_a_fault_are_told_where_to_look() -> None:
    refused = SendOutcome.advice("daemon-refused", **_SAVED)
    assert "the daemon refused the capture; see quarry.log" in refused
    failed = SendOutcome.advice("daemon-error", **_SAVED)
    assert "the daemon failed; see quarry.log or quarry doctor" in failed


@pytest.mark.parametrize(
    "label", ["daemon-unreachable", "daemon-refused", "daemon-error"]
)
class TestOnlyWhatWasSavedIsPromised:
    def test_both_saved(self, label: str) -> None:
        message = SendOutcome.advice(label, archived=True, written=True)
        assert "The raw JSONL archive and scrubbed capture are saved" in message

    def test_only_the_archive_saved(self, label: str) -> None:
        message = SendOutcome.advice(label, archived=True, written=False)
        assert "The raw JSONL archive is saved" in message
        assert "scrubbed capture" not in message

    def test_a_failed_archive_is_said_plainly_and_backfill_is_not_suggested(
        self, label: str
    ) -> None:
        message = SendOutcome.advice(label, archived=False, written=True)
        assert "the session transcript could not be archived; see quarry.log" in message
        assert "backfill" not in message
        assert "archive and" not in message

    def test_nothing_saved(self, label: str) -> None:
        message = SendOutcome.advice(label, archived=False, written=False)
        assert "the session transcript could not be archived; see quarry.log" in message
        assert "saved" not in message.replace("could not be archived", "")
        assert "backfill" not in message
