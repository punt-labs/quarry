"""Hook-entry logging through the installed console script.

Claude Code invokes ``quarry-hook`` for every handler. Each invocation must
write an INFO breadcrumb to the isolated log, including config-off and
invalid-payload paths, without corrupting the hook's JSON stdout.
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
    """A hook invocation logs its expected breadcrumb.

    LoggingConfig initializes the file handler before dispatch so every
    invocation, including an early skip, emits a HookTrace line.
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
