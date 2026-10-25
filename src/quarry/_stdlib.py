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
import re
import select
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry._frontmatter import Frontmatter
from quarry._hook_trace import HookTrace
from quarry.logging_config import LoggingConfig

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_CONFIG_FILENAME = ".punt-labs/quarry/config.md"


# ── Hook config ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class HookConfig:
    """Per-project hook configuration from ``.punt-labs/quarry/config.md``.

    ``read`` alone defaults to ``False`` — the one deliberate exception to the
    every-other-hook-defaults-``True`` pattern.  ``Read`` fires far more often
    than any other hook and has the highest secret-leak surface, so shipping it
    opt-in lets an operator confirm the filter set is clean before it captures
    unattended.
    """

    session_sync: bool = True
    web_fetch: bool = True
    compaction: bool = True
    session_end: bool = True
    web_search: bool = True
    read: bool = False
    subagent_stop: bool = True


def load_hook_config(cwd: str) -> HookConfig:
    """Load hook config from YAML-style frontmatter in the project's config file.

    Uses a pure-stdlib parser for a minimal subset of frontmatter, reading only
    the ``auto_capture`` block and its boolean fields.  This function does not
    depend on PyYAML or support arbitrary YAML.  Returns defaults if the file
    is missing, malformed, or the expected structure is absent.
    """
    path = Path(cwd) / _CONFIG_FILENAME
    if not path.is_file():
        return HookConfig()

    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return HookConfig()

    auto = Frontmatter(text).block("auto_capture")
    if auto is None:
        return HookConfig()

    # session_end and subagent_stop both capture the transcript, same as
    # compaction — an operator who disables compaction to turn off transcript
    # capture expects both to follow unless explicitly overridden.
    compaction = _bool_field(auto, "compaction", default=True)
    return HookConfig(
        session_sync=_bool_field(auto, "session_sync", default=True),
        web_fetch=_bool_field(auto, "web_fetch", default=True),
        compaction=compaction,
        session_end=_bool_field(auto, "session_end", default=compaction),
        web_search=_bool_field(auto, "web_search", default=True),
        read=_bool_field(auto, "read", default=False),
        subagent_stop=_bool_field(auto, "subagent_stop", default=compaction),
    )


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
    """Read stdin JSON, call *handler*, write stdout JSON.  Fail-open.

    Installs :class:`LoggingConfig` on entry so every handler's INFO-level
    breadcrumb (``quarry.hooks: <event>: entered ...``) actually lands in
    ``$QUARRY_LOG_DIR/quarry.log``.  The ``quarry-hook`` entry point never
    passes through the CLI's ``main`` — without this call the root logger has
    no file handler and every ``HookTrace`` line is discarded by
    :data:`logging.lastResort`, leaving the operator unable to answer
    "did my hook run?" from the file.
    """
    try:
        LoggingConfig.configure(stderr_level="WARNING")
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
_RETIRED_COMMANDS: tuple[str, ...] = ("use", "use-dev")

# A plugin name outside a conservative slug alphabet marks a manifest this
# setup does not trust; ``open`` fails open on it.  RFC-3986 unreserved minus
# ``.`` and ``~``.
_PLUGIN_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


@final
class _SessionSetup:
    """Deploy the plugin's slash commands into ``~/.claude/commands``.

    Grants no permissions: plugin startup never edits a Claude settings file.
    The allow rules are the operator's to grant, via ``quarry enable`` (#552).
    Owns the vocabulary the former free helpers shared as parameters
    (``plugin_root``, ``commands_dir``); the plugin name only selects the
    command variant (``_is_dev``).
    Constructed by :meth:`open` after a successful plugin-name read; call
    :meth:`dispatch` to run the bootstrap and collect human-readable actions.
    """

    __slots__ = (
        "_commands_dir",
        "_is_dev",
        "_plugin_root",
    )

    _commands_dir: Path
    _is_dev: bool
    _plugin_root: Path

    def __new__(cls, plugin_root: Path, plugin_name: str) -> Self:
        self = super().__new__(cls)
        self._plugin_root = plugin_root
        self._is_dev = plugin_name.endswith("-dev")
        self._commands_dir = Path.home() / ".claude" / "commands"
        return self

    @classmethod
    def open(cls, plugin_root: Path) -> Self | None:
        """Read the plugin name and construct; return ``None`` on failure."""
        try:
            plugin_name = cls._read_plugin_name(plugin_root)
        except (OSError, KeyError, ValueError):
            return None
        return cls(plugin_root, plugin_name)

    def dispatch(self) -> list[str]:
        """Deploy commands; return action lines."""
        return self._deploy_commands()

    def _deploy_commands(self) -> list[str]:
        """Deploy or update slash commands from the plugin's ``commands/``."""
        source_dir = self._plugin_root / "commands"
        if not source_dir.is_dir():
            return []
        actions = self._retire_old_commands()
        deployed: list[str] = []
        updated: list[str] = []
        for cmd_file in sorted(source_dir.glob("*.md")):
            if not self._should_deploy(cmd_file.name):
                continue
            self._install_command(cmd_file, deployed=deployed, updated=updated)
        if deployed:
            actions.append(f"Deployed commands: {' '.join(deployed)}")
        if updated:
            actions.append(f"Updated commands: {' '.join(updated)}")
        return actions

    def _install_command(
        self, cmd_file: Path, *, deployed: list[str], updated: list[str]
    ) -> None:
        """Copy *cmd_file* into ``commands_dir``; record slug on the matching list."""
        self._commands_dir.mkdir(parents=True, exist_ok=True)
        dest = self._commands_dir / cmd_file.name
        slug = f"/{cmd_file.name.removesuffix('.md')}"
        if not dest.is_file():
            shutil.copy2(cmd_file, dest)
            deployed.append(slug)
        elif not filecmp.cmp(cmd_file, dest, shallow=False):
            shutil.copy2(cmd_file, dest)
            updated.append(slug)

    def _retire_old_commands(self) -> list[str]:
        """Remove commands listed in :data:`_RETIRED_COMMANDS`."""
        actions: list[str] = []
        for old_name in _RETIRED_COMMANDS:
            dest = self._commands_dir / f"{old_name}.md"
            if dest.is_file():
                dest.unlink()
                actions.append(f"Retired /{old_name}")
        return actions

    def _should_deploy(self, name: str) -> bool:
        """Return whether a command file matches this variant (dev vs prod)."""
        if self._is_dev:
            return name.endswith("-dev.md")
        return not name.endswith("-dev.md")

    @staticmethod
    def _read_plugin_name(plugin_root: Path) -> str:
        """Read the plugin name from ``.claude-plugin/plugin.json``.

        Raise ``ValueError`` on any structural violation (top-level not a
        dict, ``name`` missing/non-string, or name outside the safe-slug
        alphabet) so :meth:`open` can catch and return ``None`` cleanly,
        honoring the fail-open contract.  The slug check refuses a hostile
        manifest name (``*``, whitespace, a path separator) outright rather
        than letting it select which command variant is deployed.
        """
        plugin_json = plugin_root / ".claude-plugin" / "plugin.json"
        with plugin_json.open() as f:
            data = json.load(f)
        if not isinstance(data, dict):
            msg = f"plugin.json top-level is not an object: {plugin_json}"
            raise ValueError(msg)
        name = data.get("name")
        if not isinstance(name, str):
            msg = f"plugin.json 'name' is not a string: {plugin_json}"
            raise ValueError(msg)
        if not _PLUGIN_NAME_PATTERN.fullmatch(name):
            msg = f"plugin.json 'name' is not a safe slug: {name!r}"
            raise ValueError(msg)
        return name


def handle_session_setup(payload: dict[str, object]) -> dict[str, object]:
    """Handle session-setup hook: deploy commands; grant no permissions.

    Emits one INFO breadcrumb (``quarry.hooks: session-setup: entered ...``)
    on every path — no-plugin-root, unreadable manifest, nothing-to-do, or a
    real capture — so the operator can see the hook fired from ``quarry.log``
    (G6).  Setup uses env vars (``CLAUDE_PLUGIN_ROOT``), not payload fields.
    """
    del payload
    trace = HookTrace("session-setup")
    # Session-setup has no per-repo config gate and no meaningful payload to
    # validate; mark both up front so every skip/capture/error breadcrumb
    # reads `config=on, payload_ok=Y` consistently, never `config=?`.
    trace.mark_config(on=True)
    trace.mark_payload(ok=True)
    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if not plugin_root_env:
        trace.skip("no-CLAUDE_PLUGIN_ROOT")
        return {}
    plugin_root = Path(plugin_root_env)
    if not plugin_root.is_dir():
        trace.skip("not-a-dir")
        return {}
    setup = _SessionSetup.open(plugin_root)
    if setup is None:
        trace.skip("plugin-name-unreadable")
        return {}
    try:
        actions = setup.dispatch()
    except OSError as exc:
        # Filesystem faults (copy/unlink/compare/write) would otherwise
        # bubble past the breadcrumb and recreate the G6 observability gap
        # for error paths — emit the trace before re-raising so run_hook's
        # fail-open still has an audit trail.
        trace.error(f"dispatch-failed:{type(exc).__name__}")
        logger.warning("session-setup: dispatch failed (%s)", type(exc).__name__)
        raise
    if not actions:
        trace.skip("nothing-to-do")
        return {}
    trace.capture(f"actions={len(actions)}")
    msg = "Quarry plugin first-run setup complete. " + " ".join(
        f"{a}." for a in actions
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": msg,
        },
    }
