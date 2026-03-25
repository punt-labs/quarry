"""Stdlib-only helpers for lightweight hook execution.

This module contains functions extracted from heavier modules
(``hooks``, ``__main__``) that only need stdlib imports.  Hook entry
points import from here to avoid pulling in pydantic, lancedb,
onnxruntime, and the full pipeline dependency tree.

Every function in this module MUST use only stdlib imports.
Adding a third-party import here defeats the entire purpose.
"""

from __future__ import annotations

import json
import logging
import os
import select
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_CONFIG_FILENAME = ".claude/quarry.local.md"


# ── Hook config ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class HookConfig:
    """Per-project hook configuration from ``.claude/quarry.local.md``."""

    session_sync: bool = True
    web_fetch: bool = True
    compaction: bool = True


def load_hook_config(cwd: str) -> HookConfig:
    """Load hook config from YAML-style frontmatter in the project's config file.

    Uses a pure-stdlib parser for a minimal subset of frontmatter, reading only
    the ``auto_capture`` block and its boolean fields.  This function does not
    depend on PyYAML or support arbitrary YAML.  Returns defaults (all enabled)
    if the file is missing, malformed, or the expected structure is absent.
    """
    path = Path(cwd) / _CONFIG_FILENAME
    if not path.is_file():
        return HookConfig()

    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return HookConfig()

    # Parse YAML frontmatter between --- delimiter lines.
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return HookConfig()

    end_index = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = i
            break

    if end_index is None:
        return HookConfig()

    frontmatter_lines = lines[1:end_index]

    # Extract auto_capture block with stdlib-only parsing.
    auto = _parse_auto_capture(frontmatter_lines)
    if auto is None:
        return HookConfig()

    return HookConfig(
        session_sync=_bool_field(auto, "session_sync", default=True),
        web_fetch=_bool_field(auto, "web_fetch", default=True),
        compaction=_bool_field(auto, "compaction", default=True),
    )


def _parse_auto_capture(lines: list[str]) -> dict[str, str] | None:
    """Extract key-value pairs under ``auto_capture:`` from frontmatter lines.

    Handles the simple nested YAML subset used by quarry config:
    ``auto_capture:\\n  key: value``.  Strips inline comments (``# ...``).
    Returns None if the block is absent.
    """
    result: dict[str, str] = {}
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped == "auto_capture:":
            in_block = True
            continue
        if in_block:
            # Skip blank / whitespace-only lines within the block.
            if not stripped:
                continue
            # Non-indented, non-blank line ends the block.
            if not line.startswith((" ", "\t")):
                break
            # Indented key: value lines are parsed; other indented lines
            # (comments, list items) are ignored without ending the block.
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                # Strip inline YAML comments.
                val = val.split("#")[0].strip()
                result[key.strip()] = val
    return result if in_block else None


# YAML 1.1 boolean aliases (case-insensitive).
_YAML_TRUE = frozenset({"true", "yes", "on"})
_YAML_FALSE = frozenset({"false", "no", "off"})


def _bool_field(data: dict[str, str], key: str, *, default: bool) -> bool:
    """Parse a boolean value from a string dict.

    Supports YAML boolean aliases (true/false, yes/no, on/off).
    Returns *default* when the key is absent.  Fails closed (returns
    ``False``) when a key is present but its value is not a recognized
    boolean — a user who explicitly sets a key intends to control the
    behavior, so an unparseable value should not silently re-enable.
    """
    val = data.get(key)
    if val is None:
        return default
    normalized = val.lower()
    if normalized in _YAML_TRUE:
        return True
    if normalized in _YAML_FALSE:
        return False
    # Present but unrecognized — fail closed to respect user intent.
    logger.warning(
        "hook-config: unrecognized boolean %r for %s, defaulting to False",
        val,
        key,
    )
    return False


# ── Hook stdin/stdout plumbing ───────────────────────────────────────


def read_hook_stdin() -> str:
    """Read hook payload from stdin without blocking.

    Claude Code may not always provide stdin (e.g. SessionStart with no
    payload).  A naive ``sys.stdin.read()`` blocks forever when no data
    and no EOF arrive.

    Uses ``select`` + ``os.read`` to consume whatever bytes are available
    within a tight timeout window, then returns.

    Falls back to ``sys.stdin.read()`` when stdin is not a real file
    descriptor (e.g. under test harnesses like ``CliRunner``).
    """
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError):
        return sys.stdin.read()

    if not select.select([fd], [], [], 0.1)[0]:
        return ""
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
        if not select.select([fd], [], [], 0.05)[0]:
            break
    return b"".join(chunks).decode()


def run_hook(handler: Callable[[dict[str, object]], dict[str, object]]) -> None:
    """Read stdin JSON, call *handler*, write stdout JSON.  Fail-open."""
    try:
        raw = read_hook_stdin()
        payload: dict[str, object] = json.loads(raw) if raw.strip() else {}
        result = handler(payload)
        sys.stdout.write(json.dumps(result))
        sys.stdout.write("\n")
    except Exception:
        logger.exception("Hook %s failed (fail-open)", handler.__name__)
        sys.stdout.write("{}\n")
