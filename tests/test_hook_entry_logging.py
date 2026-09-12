"""Hook-entry logging regression (quarry-ridg / G6).

``quarry-hook`` is the console-script entry point that Claude Code invokes
for every session-start, post-web-fetch, post-web-search, and friends.
:mod:`quarry._hook_entry` dispatches directly to a handler function — but
it never calls :meth:`quarry.logging_config.LoggingConfig.configure`.  So
every ``HookTrace`` breadcrumb the handlers emit (``quarry.hooks: <event>:
entered ...``) is discarded by ``logging.lastResort`` before it can reach
``$QUARRY_LOG_DIR/quarry.log``.  The operator's "am I even seeing my hooks
run?" question is unanswerable at production because the file is empty.

These tests spawn a real ``quarry-hook <event>`` subprocess, pipe the
canonical payload for that event on stdin, wait for exit, and grep the log
file for the expected ``entered`` line.  On v3.2.0 every parametrized case
FAILS with zero matches because the file was never opened for writing.
The fix (call ``LoggingConfig.configure(log_file=CLIENT_LOG)`` at the top
of ``_hook_entry.main``) turns every case green.

``caplog`` is deliberately NOT used here: the original G6 tests passed
because ``caplog`` attaches its own handler ahead of the app config, so
they saw the record even though the file handler was never installed.
The regression only shows through the *file*.
"""

from __future__ import annotations

import pytest

from tests.hook_fixtures import HookInvoker, hook_subprocess, load_payload

__all__ = ["hook_subprocess"]  # keep the imported fixture visible to pytest


pytestmark = pytest.mark.hook_integration


_QUARRY_RIDG_SUMMARY = (
    "quarry-ridg (G6): quarry-hook never configures logging, so HookTrace "
    "breadcrumbs are silently dropped and $QUARRY_LOG_DIR/quarry.log stays "
    "empty even when the handler ran."
)


# Per-event (payload fixture, expected substring in a written log line).  The
# expected line is the HookTrace 'entered' breadcrumb each handler emits on
# every code path (skip / capture / error), so it fires whether the payload
# passes validation or is discarded on a cwd/config skip.
_EVENT_CASES: tuple[tuple[str, str, str, str], ...] = (
    (
        "session-start",
        "webfetch/httpbin_json.json",
        "quarry.hooks: session-start: entered",
        "session-start handler runs no HookTrace when logging is off",
    ),
    (
        "post-web-fetch",
        "webfetch/httpbin_json.json",
        "quarry.hooks: post-web-fetch: entered",
        "post-web-fetch handler emits the entered breadcrumb once configured",
    ),
    (
        "post-web-search",
        "websearch/pre_2026_05.json",
        "quarry.hooks: post-web-search: entered",
        "post-web-search handler emits the entered breadcrumb once configured",
    ),
    (
        "post-read",
        "webfetch/httpbin_json.json",  # cwd-only skip suffices for the trace
        "quarry.hooks: post-read: entered",
        "post-read handler emits the entered breadcrumb on the cwd skip",
    ),
    (
        "session-end",
        "webfetch/httpbin_json.json",
        "quarry.hooks: post-session-end: entered",
        "session-end handler emits the entered breadcrumb on the cwd skip",
    ),
    (
        "subagent-stop",
        "webfetch/httpbin_json.json",
        "quarry.hooks: post-subagent-stop: entered",
        "subagent-stop handler emits the entered breadcrumb on the cwd skip",
    ),
    (
        "pre-compact",
        "webfetch/httpbin_json.json",
        "quarry.hooks: pre-compact: entered",
        "pre-compact handler emits the entered breadcrumb on the payload skip",
    ),
    (
        "session-setup",
        "webfetch/httpbin_json.json",
        "quarry.hooks: session-setup: entered",
        "session-setup handler emits the entered breadcrumb once configured",
    ),
)


@pytest.mark.parametrize(
    ("event", "payload_fixture", "expected_substr", "expectation_note"),
    _EVENT_CASES,
    ids=[case[0] for case in _EVENT_CASES],
)
def test_quarry_hook_writes_expected_log_line(
    hook_subprocess: HookInvoker,
    event: str,
    payload_fixture: str,
    expected_substr: str,
    expectation_note: str,
) -> None:
    """A ``quarry-hook <event>`` run must leave the expected line in quarry.log.

    Fails today (v3.2.0) because ``_hook_entry.main`` opens the log file
    nowhere — the file is not created and the grep returns zero matches.
    The fix installs :class:`LoggingConfig` at the top of ``main`` so every
    hook invocation opens ``quarry.log`` and emits its HookTrace line.
    """
    payload = load_payload(payload_fixture)
    run = hook_subprocess.run(event, payload)

    matches = run.grep(expected_substr)
    assert matches, (
        f"{_QUARRY_RIDG_SUMMARY}\n"
        f"  event={event!r}\n"
        f"  expected_substr={expected_substr!r} ({expectation_note})\n"
        f"  log_path={run.log_path}\n"
        f"  log_exists={run.log_path.exists()}\n"
        f"  log_line_count={len(run.log_lines)}\n"
        f"  exit_code={run.exit_code}\n"
        f"  stderr[:400]={run.stderr[:400]!r}"
    )
