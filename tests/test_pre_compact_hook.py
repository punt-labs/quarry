"""Tests for quarry.pre_compact_hook: when the hook acts and what it tells the user."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.pre_compact_hook import PreCompactHook
from quarry.send_outcome import SendOutcome
from tests.test_backfill_reach import register_for_backfill
from tests.test_hooks_agent import _make_transcript, _write_config

if TYPE_CHECKING:
    from pathlib import Path

_SEND = "quarry.daemon_capture.DaemonCaptureSender.send_capture"


def _run(tmp_path: Path, outcome: SendOutcome, **extra: str) -> dict[str, object]:
    transcript = _make_transcript(tmp_path, "work discussed today")
    payload: dict[str, object] = {
        "cwd": str(tmp_path),
        "transcript_path": str(transcript),
        "session_id": "abc12345",
        **extra,
    }
    with (
        patch("quarry.session_transcript.Path.home", return_value=tmp_path / "home"),
        patch(_SEND, return_value=outcome),
    ):
        return PreCompactHook(payload).run()


def test_a_sent_capture_says_it_is_capturing(tmp_path: Path) -> None:
    result = _run(tmp_path, SendOutcome(sent=True))
    assert str(result["systemMessage"]).startswith("Capturing")


def test_a_payload_without_a_session_id_is_skipped(tmp_path: Path) -> None:
    assert _run(tmp_path, SendOutcome(sent=True), session_id="") == {}


def test_a_disabled_compaction_capture_is_skipped(tmp_path: Path) -> None:
    _write_config(tmp_path, "---\nauto_capture:\n  compaction: false\n---\n")
    assert _run(tmp_path, SendOutcome(sent=True)) == {}


def test_a_failed_send_for_a_registered_directory_offers_backfill(
    tmp_path: Path,
) -> None:
    register_for_backfill(tmp_path)
    result = _run(tmp_path, SendOutcome.unreachable("down"))
    assert "quarry backfill-sessions" in str(result["systemMessage"])


def test_a_failed_send_for_an_unregistered_directory_does_not(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, SendOutcome.unreachable("down"))
    assert "backfill" not in str(result["systemMessage"])
