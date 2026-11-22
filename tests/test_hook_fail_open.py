"""Every hook entry point fails open: an exception never reaches Claude Code.

A hook that raised would interrupt the session.  Each entry point of
``quarry-hook`` runs its handler through :func:`quarry._stdlib.run_hook`, which
logs the failure and writes an empty response.  The test makes each handler raise
and asserts the entry point still answers ``{}`` and does not raise.
"""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from quarry import _hook_entry
from quarry._stdlib import HookConfig
from quarry.pre_compact_hook import PreCompactHook


def _raiser(_payload: dict[str, object]) -> dict[str, object]:
    msg = "handler fault"
    raise RuntimeError(msg)


_TARGETS = {
    "session-setup": "quarry._stdlib.handle_session_setup",
    "session-start": "quarry.hooks.handle_session_start",
    "post-web-fetch": "quarry.hooks.handle_post_web_fetch",
    "pre-compact": "quarry.hooks.handle_pre_compact",
    "session-end": "quarry.hooks_agent.HookAgent.session_end",
    "post-web-search": "quarry.hooks_agent.HookAgent.post_web_search",
    "post-read": "quarry.hooks_agent.HookAgent.post_read",
    "subagent-stop": "quarry.hooks_agent.HookAgent.subagent_stop",
}


def test_the_table_covers_every_entry_point() -> None:
    assert set(_TARGETS) == set(_hook_entry._HANDLERS)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("event", sorted(_TARGETS))
def test_a_raising_handler_answers_an_empty_object_and_does_not_raise(
    event: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    target = _TARGETS[event]
    replacement = staticmethod(_raiser) if ".HookAgent." in target else _raiser
    with (
        patch("quarry._stdlib.LoggingConfig.hook"),
        patch(target, new=replacement),
    ):
        _hook_entry._HANDLERS[event]()  # pyright: ignore[reportPrivateUsage]
    assert capsys.readouterr().out.strip() == "{}"
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.ERROR


@pytest.mark.parametrize("event", sorted(_TARGETS))
@pytest.mark.parametrize("raw", ["PRIVATE_BAD_JSON", "[]", "null", "", "{}"])
def test_malformed_payload_is_one_private_warning(
    event: str,
    raw: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    if event == "session-setup" and raw in {"{}", ""}:
        pytest.skip("Setup uses environment, not payload fields")
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    monkeypatch.setattr(sys, "argv", ["quarry-hook", event])
    with patch("quarry._stdlib.LoggingConfig.hook"):
        _hook_entry.main()
    assert capsys.readouterr().out == "{}\n"
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "malformed_payload"
    assert record.exc_info is None
    assert record.exc_text is None
    assert "PRIVATE_BAD_JSON" not in caplog.text
    assert "Error" not in caplog.text


def test_disabled_compaction_is_not_malformed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    with patch(
        "quarry.pre_compact_hook.load_hook_config",
        return_value=HookConfig(compaction=False),
    ):
        assert PreCompactHook(
            {"cwd": str(tmp_path), "transcript_path": "/session.jsonl", "session_id": "s"}
        ).run() == {}
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.INFO
    assert "skip:config" in caplog.text
