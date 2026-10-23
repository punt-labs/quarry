"""Tests for quarry.session_start_reply: the envelope, its trace, a refusal's text."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from quarry._hook_trace import HookTrace
from quarry.session_start_reply import SessionStartReply
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from quarry._hook_trace import Outcome


class TestOf:
    def test_it_wraps_the_context_in_the_hook_response_envelope(self) -> None:
        assert SessionStartReply.of("hello").envelope == {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "hello",
            }
        }

    def test_a_reply_is_a_capture_unless_it_says_otherwise(self) -> None:
        assert SessionStartReply.of("x").kind == "capture"


class TestTraced:
    @pytest.mark.parametrize(
        ("kind", "detail", "line"),
        [
            ("capture", "", "-> capture"),
            ("skip", "why", "-> skip:why"),
            ("error", "label", "-> error:label"),
        ],
    )
    def test_the_trace_line_names_the_kind(
        self,
        caplog: pytest.LogCaptureFixture,
        kind: Outcome,
        detail: str,
        line: str,
    ) -> None:
        reply = SessionStartReply.of("x", kind, detail)
        with caplog.at_level(logging.INFO, logger="quarry.hooks"):
            envelope = reply.traced(HookTrace("session-start"))
        assert envelope is reply.envelope
        assert caplog.messages[-1].endswith(line)


class TestRefusing:
    def test_a_registration_that_lands_has_no_reply(self, tmp_path: Path) -> None:
        project = tmp_path / "p"
        project.mkdir()
        registry = SyncRegistry(tmp_path / "registry.db")
        try:
            assert SessionStartReply.refusing(registry, project, "p") is None
            assert [r.collection for r in registry.list_registrations()] == ["p"]
        finally:
            registry.close()

    def test_a_refusal_is_a_skip_that_states_why(self, tmp_path: Path) -> None:
        project = tmp_path / "p"
        project.mkdir()
        registry = SyncRegistry(tmp_path / "registry.db")
        registry.register_directory(project, "taken")
        try:
            other = tmp_path / "q"
            other.mkdir()
            reply = SessionStartReply.refusing(registry, other, "taken")
        finally:
            registry.close()
        assert reply is not None
        assert (reply.kind, reply.detail) == ("skip", "registration-refused")
        context = str(reply.envelope["hookSpecificOutput"])
        assert "refused" in context
        assert "Nothing was registered" in context
