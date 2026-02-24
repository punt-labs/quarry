"""Tests for the hooks dispatcher and handlers."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.hooks import (
    handle_post_web_fetch,
    handle_pre_compact,
    handle_session_start,
)

runner = CliRunner()


class TestHookHandlers:
    """Each handler receives a payload dict and returns a result dict."""

    def test_session_start_returns_dict(self) -> None:
        result = handle_session_start({})
        assert isinstance(result, dict)

    def test_post_web_fetch_returns_dict(self) -> None:
        result = handle_post_web_fetch({})
        assert isinstance(result, dict)

    def test_pre_compact_returns_dict(self) -> None:
        result = handle_pre_compact({})
        assert isinstance(result, dict)

    def test_handlers_accept_arbitrary_payload(self) -> None:
        payload: dict[str, object] = {"tool_name": "WebFetch", "url": "https://x.com"}
        result = handle_post_web_fetch(payload)
        assert isinstance(result, dict)


class TestHookCLI:
    """The CLI dispatcher reads stdin JSON, calls the handler, writes stdout."""

    def test_session_start_accepts_empty_stdin(self) -> None:
        result = runner.invoke(app, ["hooks", "session-start"], input="")
        assert result.exit_code == 0
        assert json.loads(result.output) == {}

    def test_session_start_accepts_json_stdin(self) -> None:
        payload = json.dumps({"session_id": "abc"})
        result = runner.invoke(app, ["hooks", "session-start"], input=payload)
        assert result.exit_code == 0
        assert json.loads(result.output) == {}

    def test_post_web_fetch_accepts_json_stdin(self) -> None:
        payload = json.dumps({"tool_input": {"url": "https://example.com"}})
        result = runner.invoke(app, ["hooks", "post-web-fetch"], input=payload)
        assert result.exit_code == 0
        assert json.loads(result.output) == {}

    def test_pre_compact_accepts_empty_stdin(self) -> None:
        result = runner.invoke(app, ["hooks", "pre-compact"], input="")
        assert result.exit_code == 0
        assert json.loads(result.output) == {}

    def test_hooks_help(self) -> None:
        result = runner.invoke(app, ["hooks", "--help"])
        assert result.exit_code == 0
        assert "session-start" in result.output
        assert "post-web-fetch" in result.output
        assert "pre-compact" in result.output

    def test_invalid_json_is_fail_open(self) -> None:
        result = runner.invoke(app, ["hooks", "session-start"], input="not json{{{")
        assert result.exit_code == 0
        assert json.loads(result.output) == {}
