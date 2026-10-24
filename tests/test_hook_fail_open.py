"""Every hook entry point fails open: an exception never reaches Claude Code.

A hook that raised would interrupt the session.  Each entry point of
``quarry-hook`` runs its handler through :func:`quarry._stdlib.run_hook`, which
logs the failure and writes an empty response.  The test makes each handler raise
and asserts the entry point still answers ``{}`` and does not raise.
"""

from __future__ import annotations

import io
import sys
from unittest.mock import patch

import pytest

from quarry import _hook_entry


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
    event: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    target = _TARGETS[event]
    replacement = staticmethod(_raiser) if ".HookAgent." in target else _raiser
    with (
        patch("quarry._stdlib.LoggingConfig.configure"),
        patch(target, new=replacement),
    ):
        _hook_entry._HANDLERS[event]()  # pyright: ignore[reportPrivateUsage]
    assert capsys.readouterr().out.strip() == "{}"
