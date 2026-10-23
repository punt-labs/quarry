"""Every hook x every way its send can fail -> the exact trace label.

One mapping owns the label (``SendOutcome``); no hook branches on the failure kind,
so a daemon that said no is never traced as down.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import ExitStack
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from quarry.hooks import handle_post_web_fetch, handle_pre_compact
from quarry.hooks_agent import HookAgent
from quarry.send_outcome import SendOutcome
from tests.test_hooks_agent import (
    _make_dialogue,
    _make_transcript,
    _vendor_identity,
    _write_config,
)

if TYPE_CHECKING:
    from pathlib import Path

_SEND = "quarry.daemon_capture.DaemonCaptureSender.send_capture"
_OUTCOMES = {
    "refused": (SendOutcome.rejected(400, "no"), "daemon-refused"),
    "unreachable": (SendOutcome.unreachable("down"), "daemon-unreachable"),
    "server-error": (SendOutcome.rejected(503, "boom"), "daemon-error"),
    "config-error": (SendOutcome.config("misconfigured"), "config-error"),
    "malformed-reply": (SendOutcome.error("bad reply"), "daemon-error"),
}


def _session_end(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    transcript = _make_transcript(tmp_path, "work discussed today")
    with patch("quarry.session_transcript.Path.home", return_value=tmp_path / "home"):
        HookAgent.session_end(
            {
                "cwd": str(project),
                "transcript_path": str(transcript),
                "session_id": "abc12345",
                "reason": "clear",
            }
        )


def _pre_compact(tmp_path: Path) -> None:
    transcript = _make_transcript(tmp_path, "work discussed today")
    with patch("quarry.session_transcript.Path.home", return_value=tmp_path / "home"):
        handle_pre_compact(
            {
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
                "session_id": "abc12345",
            }
        )


def _subagent_stop(tmp_path: Path) -> None:
    transcript = _make_transcript(tmp_path, "sub work")
    transcript.rename(tmp_path / "sub.jsonl")
    with patch("quarry.session_transcript.Path.home", return_value=tmp_path / "home"):
        HookAgent.subagent_stop(
            {
                "cwd": str(tmp_path),
                "agent_id": "sub-1",
                "agent_type": "general-purpose",
                "agent_transcript_path": str(tmp_path / "sub.jsonl"),
            }
        )


def _web_search(tmp_path: Path) -> None:
    HookAgent.post_web_search(
        {
            "cwd": str(tmp_path),
            "tool_input": {"query": "python 3.13"},
            "tool_response": json.dumps(
                [
                    {
                        "title": "PEP 703",
                        "url": "https://peps.python.org/p/",
                        "snippet": "x",
                    }
                ]
            ),
        }
    )


def _read(tmp_path: Path) -> None:
    _write_config(tmp_path, "---\nauto_capture:\n  read: true\n---\n")
    with patch(
        "quarry._hook_trace.ReadAdmission.collection_resolver_for", return_value=None
    ):
        HookAgent.post_read(
            {
                "cwd": str(tmp_path),
                "tool_input": {"file_path": "/external/vendor-spec.pdf"},
                "tool_response": "the file content",
            }
        )


def _web_fetch(tmp_path: Path) -> None:
    handle_post_web_fetch(
        {
            "cwd": str(tmp_path),
            "tool_input": {"url": "https://example.com/docs"},
            "tool_response": json.dumps({"result": "<p>Some docs</p>"}),
        }
    )


_HOOKS: dict[str, Callable[[Path], None]] = {
    "pre-compact": _pre_compact,
    "session-end": _session_end,
    "subagent-stop": _subagent_stop,
    "post-web-search": _web_search,
    "post-read": _read,
    "post-web-fetch": _web_fetch,
}


@pytest.mark.parametrize("outcome_name", list(_OUTCOMES))
@pytest.mark.parametrize("hook_name", list(_HOOKS))
def test_a_failed_send_is_traced_with_its_own_label(
    hook_name: str,
    outcome_name: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcome, label = _OUTCOMES[outcome_name]
    caplog.set_level(logging.INFO, logger="quarry.hooks")
    with patch(_SEND, return_value=outcome):
        _HOOKS[hook_name](tmp_path)
    line = next(r for r in caplog.records if r.name == "quarry.hooks").getMessage()
    assert f"-> error:{label}" in line
    assert "-> capture" not in line


@pytest.mark.parametrize("hook_name", list(_HOOKS))
def test_a_delivered_send_is_traced_as_a_capture(
    hook_name: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="quarry.hooks")
    with patch(_SEND, return_value=SendOutcome(sent=True)):
        _HOOKS[hook_name](tmp_path)
    line = next(r for r in caplog.records if r.name == "quarry.hooks").getMessage()
    assert "-> capture" in line
    assert "-> error" not in line


_REMEMBER = "quarry.daemon_capture.DaemonCaptureSender.send_remember"
_DISTILLATION = {
    "filed": (SendOutcome(sent=True), "-> capture:distilled"),
    "refused": (SendOutcome.rejected(400, "no"), "-> capture:distill-daemon-refused"),
    "unreachable": (
        SendOutcome.unreachable("down"),
        "-> capture:distill-daemon-unreachable",
    ),
    "server-error": (
        SendOutcome.rejected(503, "boom"),
        "-> capture:distill-daemon-error",
    ),
}


@pytest.mark.parametrize("name", list(_DISTILLATION))
def test_the_distillation_has_its_own_label_when_the_raw_row_landed(
    name: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    outcome, expected = _DISTILLATION[name]
    caplog.set_level(logging.INFO, logger="quarry.hooks")
    _vendor_identity(tmp_path, "rmh")
    transcript = _make_dialogue(tmp_path / "sub.jsonl", "review", "Findings: none.")
    payload: dict[str, object] = {
        "cwd": str(tmp_path),
        "session_id": "parent",
        "agent_id": "a0f13948344b777d1",
        "agent_type": "rmh",
        "agent_transcript_path": str(transcript),
    }
    with (
        patch("quarry.session_transcript.Path.home", return_value=tmp_path / "home"),
        patch(_SEND, return_value=SendOutcome(sent=True)),
        patch(_REMEMBER, return_value=outcome),
    ):
        HookAgent.subagent_stop(payload)
    line = next(r for r in caplog.records if r.name == "quarry.hooks").getMessage()
    assert expected in line


def test_a_distillation_that_was_not_attempted_is_a_bare_capture(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="quarry.hooks")
    transcript = _make_dialogue(tmp_path / "sub.jsonl", "review", "Findings: none.")
    payload: dict[str, object] = {
        "cwd": str(tmp_path),
        "session_id": "parent",
        "agent_id": "a0f13948344b777d1",
        "agent_type": "general-purpose",  # no vendored identity: nothing to distill
        "agent_transcript_path": str(transcript),
    }
    with (
        patch("quarry.session_transcript.Path.home", return_value=tmp_path / "home"),
        patch(_SEND, return_value=SendOutcome(sent=True)),
        patch(_REMEMBER) as remember,
    ):
        HookAgent.subagent_stop(payload)
    remember.assert_not_called()
    line = next(r for r in caplog.records if r.name == "quarry.hooks").getMessage()
    assert line.endswith("-> capture")


_ADVICE = {
    "refused": "the daemon refused the capture; see quarry.log",
    "unreachable": "quarryd is not reachable",
    "server-error": "the daemon failed; see quarryd.log or quarry doctor",
    "config-error": "check QUARRY_URL / quarry login",
    "malformed-reply": "the daemon failed; see quarryd.log or quarry doctor",
}


@pytest.mark.parametrize("outcome_name", list(_ADVICE))
def test_the_pre_compact_message_follows_the_failure(
    outcome_name: str, tmp_path: Path
) -> None:
    outcome, _label = _OUTCOMES[outcome_name]
    transcript = _make_transcript(tmp_path, "work discussed today")
    with (
        patch("quarry.session_transcript.Path.home", return_value=tmp_path / "home"),
        patch(_SEND, return_value=outcome),
    ):
        result = handle_pre_compact(
            {
                "cwd": str(tmp_path),
                "transcript_path": str(transcript),
                "session_id": "abc12345",
            }
        )
    message = str(result["systemMessage"])
    assert _ADVICE[outcome_name] in message
    assert "saved" in message


def test_only_a_refused_capture_is_not_told_to_backfill(tmp_path: Path) -> None:
    transcript = _make_transcript(tmp_path, "work discussed today")
    messages = {}
    for name, (outcome, _label) in _OUTCOMES.items():
        with (
            patch(
                "quarry.session_transcript.Path.home", return_value=tmp_path / "home"
            ),
            patch(_SEND, return_value=outcome),
        ):
            result = handle_pre_compact(
                {
                    "cwd": str(tmp_path),
                    "transcript_path": str(transcript),
                    "session_id": "abc12345",
                }
            )
        messages[name] = str(result["systemMessage"])
    # A daemon that refused the capture will refuse a backfill too; every other
    # failure is recoverable, so each of those says to backfill.
    assert "backfill" not in messages["refused"]
    assert all(
        "backfill-sessions" in m for name, m in messages.items() if name != "refused"
    )


def _pre_compact_with(
    tmp_path: Path, *, archive_fails: bool, cwd: str
) -> dict[str, object]:
    transcript = _make_transcript(tmp_path, "work discussed today")
    with ExitStack() as stack:
        stack.enter_context(
            patch("quarry.session_transcript.Path.home", return_value=tmp_path / "home")
        )
        stack.enter_context(patch(_SEND, return_value=SendOutcome.unreachable("down")))
        if archive_fails:
            stack.enter_context(
                patch(
                    "quarry.transcript_reader.TranscriptReader.archive",
                    side_effect=OSError,
                )
            )
        return handle_pre_compact(
            {"cwd": cwd, "transcript_path": str(transcript), "session_id": "abc12345"}
        )


def test_a_failed_archive_is_not_reported_as_saved(tmp_path: Path) -> None:
    result = _pre_compact_with(tmp_path, archive_fails=True, cwd=str(tmp_path))
    message = str(result["systemMessage"])
    assert "the session transcript could not be archived; see quarry.log" in message
    assert "backfill" not in message
    assert "JSONL archive" not in message


def test_an_archive_without_a_local_capture_promises_only_the_archive(
    tmp_path: Path,
) -> None:
    result = _pre_compact_with(tmp_path, archive_fails=False, cwd="")
    message = str(result["systemMessage"])
    assert "The raw JSONL archive is saved" in message
    assert "scrubbed capture" not in message
