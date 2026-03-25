"""Lightweight hook entry point — bypasses full CLI import chain.

The ``quarry`` CLI (``__main__.py``) imports typer, pydantic, lancedb,
onnxruntime, and the full pipeline stack — seconds of module load
before a single line of handler code runs.

This module is the entry point for ``quarry-hook``, which dispatches
directly to handler functions via ``sys.argv``.  Each handler lazily
imports only what it needs, avoiding the full dependency tree.

Import cost: ~0.1s (stdlib only) vs ~1.5s+ (full CLI).
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from quarry._stdlib import run_hook


def main() -> None:
    """Dispatch hook commands via sys.argv — no typer overhead."""
    args = sys.argv[1:]
    if not args:
        sys.exit("Usage: quarry-hook <event>")

    event = args[0]
    handler = _HANDLERS.get(event)
    if handler is None:
        sys.exit(f"Unknown hook event: {event}")
    handler()


# ── Handler dispatch ─────────────────────────────────────────────────


def _session_start() -> None:
    from quarry.hooks import handle_session_start  # noqa: PLC0415

    run_hook(handle_session_start)


def _post_web_fetch() -> None:
    from quarry.hooks import handle_post_web_fetch  # noqa: PLC0415

    run_hook(handle_post_web_fetch)


def _pre_compact() -> None:
    from quarry.hooks import handle_pre_compact  # noqa: PLC0415

    run_hook(handle_pre_compact)


_HANDLERS: dict[str, Callable[[], None]] = {
    "session-start": _session_start,
    "post-web-fetch": _post_web_fetch,
    "pre-compact": _pre_compact,
}
