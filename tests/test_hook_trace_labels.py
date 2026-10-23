"""Every hook x every way its send can fail -> the exact trace label.

One mapping owns the label (``SendOutcome``); no hook branches on the failure kind,
so a daemon that said no is never traced as down.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
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
    "config-error": (SendOutcome.error("misconfigured"), "daemon-error"),
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
