"""Handler tests for the four new agent-lifecycle hooks.

Every handler must (a) run happy-path, (b) no-op on config-off, (c) survive a
malformed payload without raising.  ``HookAgent.subagent_stop`` gets extra
adversarial coverage because it's the only BLOCKING hook — a ``decision`` or
``block`` field in its response hangs every subagent in the session.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry._hook_trace import HookPayload, HookTrace, ReadAdmission
from quarry.hooks_agent import HookAgent

if TYPE_CHECKING:
    import pytest


class TestHookPayload:
    """HookPayload parses untrusted payload fields defensively."""

    def test_as_str_returns_string_value(self) -> None:
        assert HookPayload.as_str("hello") == "hello"

    def test_as_str_rejects_non_string(self) -> None:
        assert HookPayload.as_str(None) == ""
        assert HookPayload.as_str(42) == ""
        assert HookPayload.as_str([]) == ""

    def test_as_dir_accepts_absolute_path(self, tmp_path: Path) -> None:
        assert HookPayload.as_dir(str(tmp_path)) == str(tmp_path)

    def test_as_dir_rejects_relative(self) -> None:
        assert HookPayload.as_dir("relative/path") == ""

    def test_as_dir_rejects_non_string(self) -> None:
        assert HookPayload.as_dir(None) == ""
        assert HookPayload.as_dir(123) == ""

    def test_resolve_jsonl_returns_resolved_path(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "t.jsonl"
        jsonl.write_text("{}")
        resolved = HookPayload.resolve_jsonl(str(jsonl), label="test")
        assert resolved is not None
        assert resolved.suffix == ".jsonl"

    def test_resolve_jsonl_rejects_non_jsonl_suffix(self, tmp_path: Path) -> None:
        assert HookPayload.resolve_jsonl(str(tmp_path / "t.txt"), label="test") is None


class TestHookTrace:
    """HookTrace emits one INFO line per outcome for grep-ability."""

    def test_skip_emits_expected_shape(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging as _logging

        caplog.set_level(_logging.INFO, logger="quarry.hooks")
        trace = HookTrace("post-web-search")
        trace.mark_config(on=True)
        trace.mark_payload(ok=False)
        trace.skip("no-digest")
        records = [r for r in caplog.records if r.name == "quarry.hooks"]
        assert len(records) == 1
        msg = records[0].getMessage()
        assert "post-web-search" in msg
        assert "config=on" in msg
        assert "payload_ok=N" in msg
        assert "-> skip:no-digest" in msg

    def test_capture_emits_expected_shape(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging as _logging

        caplog.set_level(_logging.INFO, logger="quarry.hooks")
        trace = HookTrace("post-web-fetch")
        trace.mark_config(on=True)
        trace.mark_payload(ok=True)
        trace.capture()
        records = [r for r in caplog.records if r.name == "quarry.hooks"]
        assert len(records) == 1
        assert "-> capture" in records[0].getMessage()

    def test_unknown_state_renders_as_question_mark(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Emitting before marks resolves the unknown dimension to ``?``."""
        import logging as _logging

        caplog.set_level(_logging.INFO, logger="quarry.hooks")
        HookTrace("session-start").skip("cwd")
        msg = next(r for r in caplog.records if r.name == "quarry.hooks").getMessage()
        assert "config=?" in msg
        assert "payload_ok=?" in msg


class TestReadAdmission:
    """ReadAdmission groups Read-specific admission helpers."""

    def test_content_has_secret_flags_pat(self) -> None:
        assert ReadAdmission.content_has_secret("ghp_" + "a" * 36) is True

    def test_content_has_secret_ignores_clean_text(self) -> None:
        assert ReadAdmission.content_has_secret("just a plain note") is False


def _make_transcript(tmp_path: Path, text: str = "hello world") -> Path:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
    )
    return transcript


def _write_config(cwd: Path, body: str) -> None:
    config_dir = cwd / ".punt-labs" / "quarry"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.md").write_text(body)


class TestHandleSessionEnd:
    def test_happy_path_captures_transcript(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        transcript = _make_transcript(tmp_path, "work discussed today")
        with (
            patch(
                "quarry.session_transcript.Path.home",
                return_value=tmp_path / "home",
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as cap,
        ):
            result = HookAgent.session_end(
                {
                    "cwd": str(project),
                    "transcript_path": str(transcript),
                    "session_id": "abc12345-full-id",
                    "reason": "clear",
                }
            )
        assert result == {}  # never returns a systemMessage — no live user
        cap.assert_called_once()
        req = cap.call_args[0][0]
        assert req.cwd == str(project)
        assert req.session_id == "abc12345-full-id"

    def test_config_off_returns_empty(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        _write_config(project, "---\nauto_capture:\n  session_end: false\n---\n")
        transcript = _make_transcript(tmp_path)
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
            result = HookAgent.session_end(
                {
                    "cwd": str(project),
                    "transcript_path": str(transcript),
                    "session_id": "abc",
                }
            )
        assert result == {}
        cap.assert_not_called()

    def test_missing_transcript_returns_empty(self, tmp_path: Path) -> None:
        result = HookAgent.session_end({"cwd": str(tmp_path), "session_id": "abc"})
        assert result == {}

    def test_missing_cwd_fails_closed_no_capture(self) -> None:
        """No cwd means no config can be checked — fail closed, not open."""
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
            result = HookAgent.session_end(
                {"transcript_path": "/tmp/x.jsonl", "session_id": "abc"}
            )
        assert result == {}
        cap.assert_not_called()

    def test_compaction_false_disables_session_end(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        _write_config(project, "---\nauto_capture:\n  compaction: false\n---\n")
        transcript = _make_transcript(tmp_path)
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
            result = HookAgent.session_end(
                {
                    "cwd": str(project),
                    "transcript_path": str(transcript),
                    "session_id": "abc",
                }
            )
        assert result == {}
        cap.assert_not_called()

    def test_non_jsonl_transcript_returns_empty(self, tmp_path: Path) -> None:
        # Defense-in-depth against a wire-format regression.
        result = HookAgent.session_end(
            {
                "cwd": str(tmp_path),
                "transcript_path": str(tmp_path / "notes.txt"),
                "session_id": "abc",
            }
        )
        assert result == {}

    def test_malformed_payload_survives(self) -> None:
        # Non-string cwd, non-string transcript, missing session_id.
        result = HookAgent.session_end(
            {"cwd": 123, "transcript_path": None, "session_id": []}
        )
        assert result == {}

    def test_daemon_unreachable_traces_error_not_capture(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """G6: an unreachable daemon must not read as ``capture`` in the trace."""
        import logging as _logging

        caplog.set_level(_logging.INFO, logger="quarry.hooks")
        project = tmp_path / "project"
        project.mkdir()
        transcript = _make_transcript(tmp_path, "work discussed today")
        with (
            patch(
                "quarry.session_transcript.Path.home",
                return_value=tmp_path / "home",
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=False,
            ),
        ):
            HookAgent.session_end(
                {
                    "cwd": str(project),
                    "transcript_path": str(transcript),
                    "session_id": "abc12345",
                    "reason": "clear",
                }
            )
        line = next(r for r in caplog.records if r.name == "quarry.hooks").getMessage()
        assert "-> error:daemon-unreachable" in line
        assert "-> capture" not in line


class TestHandlePostWebSearch:
    def _mk_payload(self, *, cwd: str = "") -> dict[str, object]:
        return {
            "cwd": cwd or "/tmp/proj",
            "tool_input": {"query": "python 3.13"},
            "tool_response": json.dumps(
                [
                    {
                        "title": "PEP 703",
                        "url": "https://peps.python.org/pep-0703/",
                        "snippet": "GIL optional",
                    }
                ]
            ),
        }

    def test_happy_path_captures_digest(self, tmp_path: Path) -> None:
        with patch(
            "quarry.daemon_capture.DaemonCaptureSender.send_capture",
            return_value=True,
        ) as cap:
            result = HookAgent.post_web_search(self._mk_payload(cwd=str(tmp_path)))
        assert result == {}
        cap.assert_called_once()
        req = cap.call_args[0][0]
        assert "python 3.13" in req.content
        assert "PEP 703" in req.content
        assert req.format_hint == "markdown"
        assert req.cwd == str(tmp_path)

    def test_config_off_returns_empty(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "---\nauto_capture:\n  web_search: false\n---\n")
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
            result = HookAgent.post_web_search(self._mk_payload(cwd=str(tmp_path)))
        assert result == {}
        cap.assert_not_called()

    def test_no_results_returns_empty(self) -> None:
        payload: dict[str, object] = {
            "tool_input": {"query": "x"},
            "tool_response": json.dumps([]),
        }
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
            result = HookAgent.post_web_search(payload)
        assert result == {}
        cap.assert_not_called()

    def test_malformed_payload_survives(self) -> None:
        result = HookAgent.post_web_search({"tool_input": None, "tool_response": 42})
        assert result == {}

    def test_daemon_unreachable_traces_error_not_capture(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """G6: a false send_capture return must trace error, not capture."""
        import logging as _logging

        caplog.set_level(_logging.INFO, logger="quarry.hooks")
        with patch(
            "quarry.daemon_capture.DaemonCaptureSender.send_capture",
            return_value=False,
        ):
            HookAgent.post_web_search(self._mk_payload(cwd=str(tmp_path)))
        line = next(r for r in caplog.records if r.name == "quarry.hooks").getMessage()
        assert "-> error:daemon-unreachable" in line
        assert "-> capture" not in line


class TestHandlePostReadFilterBranches:
    """Each of the four filter branches rejects independently."""

    def _payload(self, tmp_path: Path, file_path: str) -> dict[str, object]:
        # `read` defaults to False — enable via config so the handler runs.
        _write_config(tmp_path, "---\nauto_capture:\n  read: true\n---\n")
        return {
            "cwd": str(tmp_path),
            "tool_input": {"file_path": file_path},
            "tool_response": "the file content",
        }

    def test_secret_path_denylist_rejects(self, tmp_path: Path) -> None:
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
            result = HookAgent.post_read(self._payload(tmp_path, "/home/u/.ssh/id_rsa"))
        assert result == {}
        cap.assert_not_called()

    def test_extension_allowlist_rejects(self, tmp_path: Path) -> None:
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
            result = HookAgent.post_read(self._payload(tmp_path, "/tmp/x.py"))
        assert result == {}
        cap.assert_not_called()

    def test_size_cap_rejects(self, tmp_path: Path) -> None:
        payload = self._payload(tmp_path, "/tmp/big.pdf")
        payload["tool_response"] = "x" * (300 * 1024)  # 300 KB, over the 200 KB cap
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
            result = HookAgent.post_read(payload)
        assert result == {}
        cap.assert_not_called()

    def test_in_tree_exclusion_rejects(self, tmp_path: Path) -> None:
        """A path under a registered directory is skipped as already-indexed."""

        class _FakeResolver:
            def covering_registration(self, _cwd: str) -> object:
                class _Reg:
                    directory = str(tmp_path)

                return _Reg()

        payload = self._payload(tmp_path, str(tmp_path / "docs" / "readme.md"))
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "readme.md").write_text("hi")
        with (
            patch(
                "quarry._hook_trace.ReadAdmission.collection_resolver_for",
                return_value=_FakeResolver(),
            ),
            patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap,
        ):
            result = HookAgent.post_read(payload)
        assert result == {}
        cap.assert_not_called()

    def test_content_matching_secret_pattern_rejects(self, tmp_path: Path) -> None:
        """Client-side scrub pre-check: a filter-passing file with a real
        secret pattern embedded is still not captured."""
        payload = self._payload(tmp_path, "/external/notes.md")
        payload["tool_response"] = "ghp_" + "a" * 36
        with (
            patch(
                "quarry._hook_trace.ReadAdmission.collection_resolver_for",
                return_value=None,
            ),
            patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap,
        ):
            result = HookAgent.post_read(payload)
        assert result == {}
        cap.assert_not_called()

    def test_happy_path_captures(self, tmp_path: Path) -> None:
        payload = self._payload(tmp_path, "/external/vendor-spec.pdf")
        with (
            patch(
                "quarry._hook_trace.ReadAdmission.collection_resolver_for",
                return_value=None,
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as cap,
        ):
            result = HookAgent.post_read(payload)
        assert result == {}
        cap.assert_called_once()
        req = cap.call_args[0][0]
        assert req.document_name == "/external/vendor-spec.pdf"
        assert req.format_hint == "auto"


class TestHandlePostRead:
    def test_default_off_returns_empty(self, tmp_path: Path) -> None:
        # HookConfig.read defaults False — even without a config file, no capture.
        payload: dict[str, object] = {
            "cwd": str(tmp_path),
            "tool_input": {"file_path": "/tmp/doc.md"},
            "tool_response": "hi",
        }
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
            result = HookAgent.post_read(payload)
        assert result == {}
        cap.assert_not_called()

    def test_missing_cwd_returns_empty(self) -> None:
        result = HookAgent.post_read({"tool_input": {"file_path": "/tmp/x.md"}})
        assert result == {}

    def test_malformed_payload_survives(self) -> None:
        result = HookAgent.post_read({"tool_input": 42, "tool_response": None})
        assert result == {}

    def test_daemon_unreachable_traces_error_not_capture(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """G6: a false send_capture return must trace error, not capture."""
        import logging as _logging

        caplog.set_level(_logging.INFO, logger="quarry.hooks")
        _write_config(tmp_path, "---\nauto_capture:\n  read: true\n---\n")
        payload: dict[str, object] = {
            "cwd": str(tmp_path),
            "tool_input": {"file_path": "/external/vendor-spec.pdf"},
            "tool_response": "the file content",
        }
        with (
            patch(
                "quarry._hook_trace.ReadAdmission.collection_resolver_for",
                return_value=None,
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=False,
            ),
        ):
            HookAgent.post_read(payload)
        line = next(r for r in caplog.records if r.name == "quarry.hooks").getMessage()
        assert "-> error:daemon-unreachable" in line
        assert "-> capture" not in line


_CONFIRMED_SUBAGENT_PAYLOAD: dict[str, object] = {
    "session_id": "304fdeb9-9e2b-4146-b9fd-23965db33ce6",
    "transcript_path": "/parent/session.jsonl",
    "cwd": "/home/u/proj",
    "prompt_id": "bc89f018-dae4-4d2e-aefc-d1e6baa4987c",
    "permission_mode": "default",
    "agent_id": "a0f13948344b777d1",
    "agent_type": "general-purpose",
    "effort": {"level": "high"},
    "hook_event_name": "SubagentStop",
    "stop_hook_active": False,
    "agent_transcript_path": "/subagent/agent-a0f13948344b777d1.jsonl",
    "last_assistant_message": "ok",
    "background_tasks": [],
    "session_crons": [],
}


class TestHandleSubagentStop:
    """SubagentStop is a BLOCKING hook — the handler must NEVER emit
    a decision or block field, no matter what the payload contains."""

    def test_response_never_contains_decision_field(self, tmp_path: Path) -> None:
        """Under crafted-adversarial payloads the response is bare ``{}``."""
        crafted: list[dict[str, object]] = [
            {},
            {"agent_id": "a1", "agent_transcript_path": "/x/x.jsonl"},
            _CONFIRMED_SUBAGENT_PAYLOAD,
            # non-string carriers, wrong-suffix transcript, missing fields
            {"agent_id": None, "agent_transcript_path": "/x/x.txt"},
            {"agent_id": [], "agent_transcript_path": 42},
        ]
        for payload in crafted:
            with patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ):
                result = HookAgent.subagent_stop(payload)
            assert "decision" not in result, (
                f"BLOCKING regression on {payload}: decision must never appear"
            )
            assert "block" not in result
            # Handler must not emit stopReason/continue either — no fields at all.
            assert result == {}, f"expected {{}}, got {result} for {payload}"

    def test_archives_agent_transcript_path_not_parent(self, tmp_path: Path) -> None:
        """R4b: agent_transcript_path is the subagent scope, distinct from
        transcript_path (which is the parent's)."""
        parent = _make_transcript(tmp_path, "parent chat")
        parent.rename(tmp_path / "parent.jsonl")
        subagent = _make_transcript(tmp_path, "subagent chat")
        subagent.rename(tmp_path / "subagent.jsonl")

        payload: dict[str, object] = {
            "cwd": str(tmp_path),
            "session_id": "parent-session-id",
            "transcript_path": str(tmp_path / "parent.jsonl"),
            "agent_id": "sub-1",
            "agent_type": "general-purpose",
            "agent_transcript_path": str(tmp_path / "subagent.jsonl"),
        }
        with (
            patch(
                "quarry.session_transcript.Path.home",
                return_value=tmp_path / "home",
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as cap,
        ):
            result = HookAgent.subagent_stop(payload)
        assert result == {}
        cap.assert_called_once()
        req = cap.call_args[0][0]
        # The session_id on the wire IS the agent_id (not the parent's).
        assert req.session_id == "sub-1"
        # And the archived text came from the subagent transcript.
        assert "subagent chat" in req.content
        assert "parent chat" not in req.content

    def test_uses_agent_type_as_handle(self, tmp_path: Path) -> None:
        subagent = _make_transcript(tmp_path, "sub")
        subagent.rename(tmp_path / "sub.jsonl")
        payload: dict[str, object] = {
            "cwd": str(tmp_path),
            "agent_id": "sub-2",
            "agent_type": "rmh",
            "agent_transcript_path": str(tmp_path / "sub.jsonl"),
        }
        with (
            patch(
                "quarry.session_transcript.Path.home",
                return_value=tmp_path / "home",
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=True,
            ) as cap,
        ):
            result = HookAgent.subagent_stop(payload)
        assert result == {}
        req = cap.call_args[0][0]
        assert req.agent_handle == "rmh"

    def test_config_off_returns_empty(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "---\nauto_capture:\n  subagent_stop: false\n---\n")
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
            result = HookAgent.subagent_stop(
                {
                    "cwd": str(tmp_path),
                    "agent_id": "x",
                    "agent_transcript_path": "/x.jsonl",
                }
            )
        assert result == {}
        cap.assert_not_called()

    def test_missing_cwd_fails_closed_no_capture(self) -> None:
        """No cwd means no config can be checked — fail closed, not open."""
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
            result = HookAgent.subagent_stop(
                {"agent_id": "x", "agent_transcript_path": "/x.jsonl"}
            )
        assert result == {}
        cap.assert_not_called()

    def test_compaction_false_disables_subagent_stop(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "---\nauto_capture:\n  compaction: false\n---\n")
        subagent = _make_transcript(tmp_path)
        with patch("quarry.daemon_capture.DaemonCaptureSender.send_capture") as cap:
            result = HookAgent.subagent_stop(
                {
                    "cwd": str(tmp_path),
                    "agent_id": "x",
                    "agent_transcript_path": str(subagent),
                }
            )
        assert result == {}
        cap.assert_not_called()

    def test_malformed_payload_survives(self, caplog: pytest.LogCaptureFixture) -> None:
        result = HookAgent.subagent_stop(
            {"cwd": 42, "agent_id": None, "agent_transcript_path": 3.14}
        )
        assert result == {}

    def test_daemon_unreachable_traces_error_not_capture(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """G6: a subagent transcript send that fails must trace error, not capture."""
        import logging as _logging

        caplog.set_level(_logging.INFO, logger="quarry.hooks")
        subagent = _make_transcript(tmp_path, "sub work")
        subagent.rename(tmp_path / "sub.jsonl")
        payload: dict[str, object] = {
            "cwd": str(tmp_path),
            "agent_id": "sub-1",
            "agent_type": "general-purpose",
            "agent_transcript_path": str(tmp_path / "sub.jsonl"),
        }
        with (
            patch(
                "quarry.session_transcript.Path.home",
                return_value=tmp_path / "home",
            ),
            patch(
                "quarry.daemon_capture.DaemonCaptureSender.send_capture",
                return_value=False,
            ),
        ):
            HookAgent.subagent_stop(payload)
        line = next(r for r in caplog.records if r.name == "quarry.hooks").getMessage()
        assert "-> error:daemon-unreachable" in line
        assert "-> capture" not in line
