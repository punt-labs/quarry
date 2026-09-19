"""Behaviour of :class:`quarry.subagent_capture.SubagentCapture`.

Two rows from one SubagentStop: the raw transcript always, the distilled report
only for a registered identity with something to say. The identity check reads
the vendored tree under *cwd*; the daemon is patched at the sender boundary.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from quarry.session_transcript import TranscriptSource
from quarry.subagent_capture import SubagentCapture
from quarry.transcript_reader import TranscriptReader

if TYPE_CHECKING:
    from pathlib import Path


def _turn(role: str, text: str) -> dict[str, object]:
    return {
        "type": role,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _vendor_identity(root: Path, handle: str) -> None:
    identities = root / ".punt-labs" / "ethos" / "identities"
    identities.mkdir(parents=True, exist_ok=True)
    (identities / f"{handle}.yaml").write_text(f"handle: {handle}\nkind: agent\n")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A repo-shaped cwd with ``rmh`` vendored as an identity."""
    _vendor_identity(tmp_path, "rmh")
    return tmp_path


def _capture(
    project: Path, *records: dict[str, object], agent_type: str = "rmh"
) -> SubagentCapture:
    transcript = project / "agent.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in records))
    return SubagentCapture(
        TranscriptSource(str(project), "a0f13948-agent", transcript, "subagent-stop"),
        agent_type=agent_type,
        parent_session_id="parent-1",
    )


class TestRegisteredIdentity:
    def test_files_raw_capture_and_distilled_report(self, project: Path) -> None:
        capture = _capture(project, _turn("user", "go"), _turn("assistant", "Done."))
        with (
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as raw,
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_remember",
                return_value=True,
            ) as memory,
        ):
            outcome = capture.capture()

        assert outcome.handle == "rmh"
        assert outcome.distilled is True
        assert outcome.raw.sent is True
        raw_req = raw.call_args[0][0]
        assert raw_req.agent_handle == "rmh"
        assert raw_req.summary == "Done."
        assert raw_req.session_id == "a0f13948-agent"
        mem_req = memory.call_args[0][0]
        assert mem_req.name == "subagent-a0f13948-report"
        assert mem_req.agent_handle == "rmh"

    def test_unreachable_daemon_leaves_distilled_false(self, project: Path) -> None:
        capture = _capture(project, _turn("assistant", "Done."))
        with (
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_remember",
                return_value=False,
            ),
        ):
            outcome = capture.capture()
        assert outcome.raw.sent is True
        assert outcome.distilled is False

    def test_no_assistant_turn_files_no_report(self, project: Path) -> None:
        capture = _capture(project, _turn("user", "only the user spoke"))
        with (
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ),
            patch("quarry.daemon_capture.DaemonCaptureSender.send_remember") as memory,
        ):
            outcome = capture.capture()
        assert outcome.raw.text_captured is True
        assert outcome.distilled is False
        memory.assert_not_called()

    def test_empty_transcript_files_nothing(self, project: Path) -> None:
        capture = _capture(project)
        with (
            patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as raw,
            patch("quarry.daemon_capture.DaemonCaptureSender.send_remember") as memory,
        ):
            outcome = capture.capture()
        assert outcome.raw.text_captured is False
        assert outcome.distilled is False
        raw.assert_not_called()
        memory.assert_not_called()


class TestSingleParse:
    def test_one_reader_serves_both_rows(self, project: Path) -> None:
        """The transcript is parsed once: the report and the raw row share a reader."""
        capture = _capture(project, _turn("user", "go"), _turn("assistant", "Done."))
        with (
            patch(
                "quarry.subagent_capture.TranscriptReader", wraps=TranscriptReader
            ) as reader_cls,
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as raw,
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_remember",
                return_value=True,
            ) as memory,
        ):
            outcome = capture.capture()
        assert reader_cls.call_count == 1
        assert outcome.distilled is True
        assert "Done." in raw.call_args[0][0].content
        assert "Done." in memory.call_args[0][0].content


class TestUnregisteredAgentType:
    def test_bare_agent_is_unattributed_and_not_distilled(self, project: Path) -> None:
        capture = _capture(
            project,
            _turn("assistant", "Findings: none."),
            agent_type="general-purpose",
        )
        with (
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as raw,
            patch("quarry.daemon_capture.DaemonCaptureSender.send_remember") as memory,
        ):
            outcome = capture.capture()
        assert outcome.handle == ""
        assert outcome.distilled is False
        assert raw.call_args[0][0].agent_handle == ""
        memory.assert_not_called()

    def test_never_falls_back_to_the_repo_pin(self, project: Path) -> None:
        """The pin names the leader; a subagent's row must not be filed as theirs."""
        (project / ".punt-labs" / "ethos.yaml").write_text("agent: claude\n")
        capture = _capture(
            project, _turn("assistant", "ok"), agent_type="general-purpose"
        )
        with (
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as raw,
            patch("quarry.daemon_capture.DaemonCaptureSender.send_remember"),
        ):
            outcome = capture.capture()
        assert outcome.handle == ""
        assert raw.call_args[0][0].agent_handle == ""
