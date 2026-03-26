"""Stdlib-only helpers for lightweight hook execution.

This module contains functions extracted from heavier modules
(``hooks``, ``__main__``) that only need stdlib imports.  Hook entry
points import from here to avoid pulling in pydantic, lancedb,
onnxruntime, and the full pipeline dependency tree.

Every function in this module MUST use only stdlib imports.
Adding a third-party import here defeats the entire purpose.
"""

from __future__ import annotations

import filecmp
import json
import logging
import os
import select
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_CONFIG_FILENAME = ".punt-labs/quarry/config.md"


# ── Hook config ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class HookConfig:
    """Per-project hook configuration from ``.punt-labs/quarry/config.md``."""

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


# ── Session setup (plugin bootstrap) ─────────────────────────────────

# Commands removed or renamed — add old filenames here to auto-retire.
_RETIRED_COMMANDS: list[str] = []


def _read_plugin_name(plugin_root: Path) -> str:
    """Read the plugin name from ``.claude-plugin/plugin.json``."""
    plugin_json = plugin_root / ".claude-plugin" / "plugin.json"
    with plugin_json.open() as f:
        data = json.load(f)
    name: str = data["name"]
    return name


def _retire_old_commands(commands_dir: Path) -> list[str]:
    """Remove commands listed in ``_RETIRED_COMMANDS`` from the user's directory."""
    actions: list[str] = []
    for old_name in _RETIRED_COMMANDS:
        dest = commands_dir / f"{old_name}.md"
        if dest.is_file():
            dest.unlink()
            actions.append(f"Retired /{old_name}")
    return actions


def _should_deploy(name: str, *, is_dev: bool) -> bool:
    """Return whether a command file should be deployed for this plugin variant."""
    if is_dev:
        return name.endswith("-dev.md")
    return not name.endswith("-dev.md")


def _deploy_commands(
    plugin_root: Path,
    plugin_name: str,
    commands_dir: Path,
) -> list[str]:
    """Deploy or update slash commands from the plugin's commands/ directory.

    Dev plugins (name ends with ``-dev``) only deploy ``*-dev.md`` commands;
    prod plugins skip ``*-dev.md`` files.

    Returns a list of human-readable action strings.
    """
    source_dir = plugin_root / "commands"
    if not source_dir.is_dir():
        return []

    actions = _retire_old_commands(commands_dir)
    is_dev = plugin_name.endswith("-dev")
    deployed: list[str] = []
    updated: list[str] = []

    for cmd_file in sorted(source_dir.glob("*.md")):
        if not _should_deploy(cmd_file.name, is_dev=is_dev):
            continue

        commands_dir.mkdir(parents=True, exist_ok=True)
        dest = commands_dir / cmd_file.name
        slug = f"/{cmd_file.name.removesuffix('.md')}"

        if not dest.is_file():
            shutil.copy2(cmd_file, dest)
            deployed.append(slug)
        elif not filecmp.cmp(cmd_file, dest, shallow=False):
            shutil.copy2(cmd_file, dest)
            updated.append(slug)

    if deployed:
        actions.append(f"Deployed commands: {' '.join(deployed)}")
    if updated:
        actions.append(f"Updated commands: {' '.join(updated)}")

    return actions


def _allow_mcp_tools(plugin_name: str, settings_path: Path) -> str | None:
    """Add the plugin's MCP tool pattern to ``permissions.allow`` if missing.

    Returns an action string if the permission was added, None otherwise.
    """
    if not settings_path.is_file():
        return None

    try:
        text = settings_path.read_text()
        settings = json.loads(text)
    except (OSError, ValueError):
        return None

    tool_pattern = f"mcp__plugin_{plugin_name}_quarry__*"
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
        settings["permissions"] = permissions

    allow_list = permissions.get("allow")
    if not isinstance(allow_list, list):
        allow_list = []
        permissions["allow"] = allow_list

    # Check if any existing entry already covers this plugin's tools.
    tool_prefix = f"mcp__plugin_{plugin_name}_quarry__"
    for entry in allow_list:
        if isinstance(entry, str) and tool_prefix in entry:
            return None

    allow_list.append(tool_pattern)
    tmp_path = settings_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(settings, indent=2) + "\n")
        tmp_path.replace(settings_path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        return None

    return f"Auto-allowed {plugin_name} MCP tools in permissions"


def handle_session_setup(payload: dict[str, object]) -> dict[str, object]:
    """Handle session-setup hook: deploy commands and allow MCP tools.

    This is the Python replacement for the former ``session-start.sh``
    shell script.  It reads the plugin root from ``CLAUDE_PLUGIN_ROOT``,
    deploys slash commands to ``~/.claude/commands/``, and ensures the
    plugin's MCP tools are in the user's ``settings.json`` allow list.

    Returns ``additionalContext`` when actions were taken.
    """
    _ = payload  # Signature required by run_hook; setup uses env vars.
    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if not plugin_root_env:
        logger.debug("session-setup: CLAUDE_PLUGIN_ROOT not set, skipping")
        return {}

    plugin_root = Path(plugin_root_env)
    if not plugin_root.is_dir():
        logger.debug("session-setup: plugin root not a directory: %s", plugin_root)
        return {}

    try:
        plugin_name = _read_plugin_name(plugin_root)
    except (OSError, KeyError, ValueError):
        logger.debug("session-setup: could not read plugin name")
        return {}

    actions: list[str] = []

    commands_dir = Path.home() / ".claude" / "commands"
    actions.extend(_deploy_commands(plugin_root, plugin_name, commands_dir))

    settings_path = Path.home() / ".claude" / "settings.json"
    mcp_action = _allow_mcp_tools(plugin_name, settings_path)
    if mcp_action:
        actions.append(mcp_action)

    if not actions:
        return {}

    msg = "Quarry plugin first-run setup complete. " + " ".join(
        f"{a}." for a in actions
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": msg,
        },
    }
