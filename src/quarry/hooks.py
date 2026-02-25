"""Hook handlers for Claude Code integration.

Each handler receives the parsed stdin JSON from Claude Code and returns
a dict that will be serialized to stdout.  All handlers are fail-open:
exceptions are logged but never propagated to the caller.

Hook events:
    session-start    — SessionStart: auto-register and sync the current repo.
    post-web-fetch   — PostToolUse on WebFetch: auto-ingest fetched URLs.
    pre-compact      — PreCompact: capture compaction summaries.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def handle_session_start(payload: dict[str, object]) -> dict[str, object]:
    """Handle SessionStart hook.

    Future: auto-register the current repo and sync, then inject
    additionalContext telling Claude that quarry is available.
    """
    logger.debug("session-start hook received payload: %s", payload)
    return {}


def handle_post_web_fetch(payload: dict[str, object]) -> dict[str, object]:
    """Handle PostToolUse on WebFetch.

    Future: extract the fetched URL and queue it for background ingestion
    into the web-captures collection.
    """
    logger.debug("post-web-fetch hook received payload: %s", payload)
    return {}


def handle_pre_compact(payload: dict[str, object]) -> dict[str, object]:
    """Handle PreCompact hook.

    Future: capture the compaction summary as a searchable document
    in the session-notes collection.
    """
    logger.debug("pre-compact hook received payload: %s", payload)
    return {}
