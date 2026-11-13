"""Claude Code's wiring of the quarry MCP server: write it, and check it."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import ClassVar, Self, final

from quarry.atomic_file import AtomicFile
from quarry.doctor_failure import CheckFailure
from quarry.file_lock import FileLock
from quarry.home_paths import QuarryHome
from quarry.json_file import ConfigError, JsonFile
from quarry.mcp_constants import MCP_SERVER_NAME as _MCP_SERVER_NAME
from quarry.results import CheckResult

_SCOPE_USER = ("--scope", "user")


@dataclass(frozen=True, slots=True)
class _ClaudeCall:
    """The path and deadline shared by every ``claude`` subprocess invocation."""

    claude_path: str
    deadline: float


@final
class ClaudeCodeMcp:
    """Claude Code's wiring of the quarry MCP server: write it, and check it."""

    # Long enough for a slow machine, short enough that an install never hangs;
    # the total bounds the up to three calls a stale-entry replace makes.
    _CLAUDE_TIMEOUT_S: ClassVar[float] = 60.0
    _TOTAL_TIMEOUT_S: ClassVar[float] = 120.0
    _PLUGIN_KEY: ClassVar[str] = "quarry@punt-labs"

    __slots__ = ("_plugins_path",)

    _plugins_path: Path

    def __new__(cls, plugins_path: Path | None = None) -> Self:
        self = super().__new__(cls)
        self._plugins_path = plugins_path or (
            QuarryHome.home() / ".claude" / "plugins" / "installed_plugins.json"
        )
        return self

    def configure(self) -> CheckResult:
        """Register the quarry server -- a no-op when the plugin already declares it.

        The plugin's own ``plugin.json`` declares ``mcpServers.quarry`` ahead
        of a user-scope entry written here, so adding one on top just shadows
        the plugin (tool-enable-disable.md §2.6/§2.8); :meth:`check` passing
        is reported straight through -- only ``--no-plugin`` needs the add.

        Registers at ``--scope user`` (Claude Code's machine-wide scope) rather
        than the ``local`` default, which is scoped to whatever directory
        ``quarry install``/``doctor`` happened to run from. Quarry is a
        once-per-machine install, so its MCP entry must not depend on cwd. Note:
        a `local`-scope entry from an older install (or a per-project
        `.mcp.json`) shadows the user-scope entry for that one project — Claude
        Code resolves local before user. Remove any stray local-scope entry with
        ``claude mcp remove quarry --scope local`` from inside that project.

        Add-first, remove-only-if-blocked: try ``claude mcp add`` and act on the
        result. A fresh slot succeeds outright. Only when the add reports the entry
        already exists (e.g. the retired mcp-proxy shim shadowing the direct entry)
        do we remove and re-add — so a failing add on a fresh install never leaves
        Claude Code with no quarry entry. If the re-add fails after a removal, that
        is surfaced loudly (the user must re-run), never reported as configured.

        Always returns a result: a ``claude`` that cannot be run or does not answer
        within the timeout is a failed check, not an exception.
        """
        if self.check().passed:
            return CheckResult(
                name="Claude Code MCP", passed=True, message="configured (via plugin)"
            )
        claude_path = shutil.which("claude")
        if claude_path is None:
            return self._failure("claude CLI not found on PATH")
        try:
            return self._register(claude_path)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._failure(
                f"claude could not be run ({CheckFailure.detail(exc)})"
            )

    def check(self) -> CheckResult:
        """Check whether quarry MCP is configured in Claude Code (read-only).

        Reads the plugin registry JSON directly instead of shelling out to
        ``claude mcp list``, which spawns every configured MCP server and
        exceeds the timeout when many plugins are installed. :meth:`configure`
        calls this first, so the two-store split the old NOTE here used to
        warn about no longer applies: only the ``--no-plugin`` path still
        writes through ``claude mcp add``, a store this check never reads.
        """
        try:
            if not self._plugins_path.exists():
                return self._failure("no plugin registry found")
            return self._read_registry()
        except (OSError, ValueError) as exc:
            return self._failure(
                f"config error: {JsonFile.why(exc, self._plugins_path)}"
            )

    def _read_registry(self) -> CheckResult:
        """Return whether the registry lists a quarry plugin whose manifest says so."""
        registry = self._plugins_path
        plugins = JsonFile.object_at(JsonFile.load(registry), "plugins", registry)
        entries = plugins.get(self._PLUGIN_KEY)
        if not entries:
            return self._failure("not configured (run 'quarry install')")
        # Verify the install path contains a valid plugin manifest with an
        # mcpServers entry for quarry.  This catches stale registry entries where
        # the plugin directory was deleted or corrupted.
        if not isinstance(entries, list) or not isinstance(entries[0], dict):
            msg = f"{registry.name}: the quarry plugin entry is malformed"
            raise ConfigError(msg)
        return self._verify_install_path(entries[0])

    def _verify_install_path(self, entry: dict[str, object]) -> CheckResult:
        """Return whether *entry*'s ``installPath`` has a valid quarry manifest."""
        raw_path = entry.get("installPath", "")
        if not raw_path or not isinstance(raw_path, str):
            return self._failure("plugin registry has empty installPath")
        install_path = Path(raw_path)
        plugin_json = install_path / ".claude-plugin" / "plugin.json"
        if not plugin_json.exists():
            return self._failure(
                f"plugin files missing at {CheckFailure.line(install_path.name)}"
            )
        manifest = JsonFile.load(plugin_json)
        servers = JsonFile.object_at(manifest, "mcpServers", plugin_json)
        if _MCP_SERVER_NAME not in servers:
            return self._failure("plugin manifest missing quarry MCP server entry")
        return CheckResult(name="Claude Code MCP", passed=True, message="configured")

    def _register(self, claude_path: str) -> CheckResult:
        """Add the quarry server; fall through to replace a stale blocking entry."""
        call = _ClaudeCall(claude_path, time.monotonic() + self._TOTAL_TIMEOUT_S)
        run = partial(self._run, call)
        add_argv = [
            "mcp",
            "add",
            _MCP_SERVER_NAME,
            *_SCOPE_USER,
            "--",
            *self._command(),
        ]
        result = run(*add_argv)
        if result.returncode == 0:
            return self._configured_ok()
        if "already exists" not in result.stderr:
            return self._failure(f"claude mcp add failed: {self._said(result)}")
        # An entry already exists and blocks the direct add. Remove it and re-add;
        # only now — with the entry confirmed present — do we risk a removal.
        return self._replace_stale_entry(run, add_argv)

    def _replace_stale_entry(
        self, run: Callable[..., subprocess.CompletedProcess[str]], add_argv: list[str]
    ) -> CheckResult:
        """Remove the entry blocking the add, then re-add; surface either failure."""
        try:
            remove = run("mcp", "remove", _MCP_SERVER_NAME, *_SCOPE_USER)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._failure(
                "the remove of the stale quarry MCP entry did not finish "
                f"({CheckFailure.detail(exc)}), so the entry may be gone: "
                "re-run 'quarry install' or "
                "'claude mcp add quarry --scope user -- quarry mcp'."
            )
        if remove.returncode != 0:
            # The remove failed: the stale entry is likely still present, so do NOT
            # re-add blindly or claim a removal that did not happen.
            return self._failure(
                "a stale quarry MCP entry blocks the add but could not be removed: "
                f"{self._said(remove)}. Inspect with 'claude mcp list' — a stale "
                "'local'-scope entry can shadow the 'user'-scope one and cause this "
                "exact failure; try 'claude mcp remove quarry --scope local' first, "
                "then re-run 'quarry install'."
            )
        try:
            retry = run(*add_argv)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._failure(self._readd_failed(CheckFailure.detail(exc)))
        if retry.returncode == 0:
            return self._configured_ok()
        return self._failure(self._readd_failed(self._said(retry)))

    @staticmethod
    def _configured_ok() -> CheckResult:
        """Return the passing result for a successful user-scope add."""
        return CheckResult(
            name="Claude Code MCP", passed=True, message="configured (scope: user)"
        )

    @staticmethod
    def _readd_failed(why: str) -> str:
        """Say, loudly, that the stale entry is gone and the new one is not in."""
        return (
            f"removed the stale quarry MCP entry but the re-add failed: {why}. "
            "Re-run 'quarry install' or "
            "'claude mcp add quarry --scope user -- quarry mcp'."
        )

    @staticmethod
    def _said(result: subprocess.CompletedProcess[str]) -> str:
        """Return what ``claude`` said (stderr, else stdout) as one bounded line."""
        return CheckFailure.cause(result.stderr) or CheckFailure.cause(result.stdout)

    @classmethod
    def _run(cls, call: _ClaudeCall, *argv: str) -> subprocess.CompletedProcess[str]:
        """Run ``claude`` with *argv* before *call*'s deadline (the one S603 site).

        Its output is decoded as UTF-8 with replacement, so bytes that are not
        text cannot raise.
        """
        remaining = call.deadline - time.monotonic()
        if remaining <= 0:
            elapsed = cls._TOTAL_TIMEOUT_S - remaining
            raise subprocess.TimeoutExpired([call.claude_path, *argv], elapsed)
        timeout = min(cls._CLAUDE_TIMEOUT_S, remaining)
        return subprocess.run(  # noqa: S603
            [call.claude_path, *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )

    @staticmethod
    def _command() -> list[str]:
        """Return the command that runs the quarry MCP server directly.

        ``quarry mcp`` is a stdio FastMCP client of the daemon, which it reaches
        through ``QuarryClient``: no shim and no ``sh -c`` wrapper.  Remote access
        is carried by the client's own TLS + pinned-CA login config.
        """
        return ["quarry", "mcp"]

    @staticmethod
    def _failure(message: str) -> CheckResult:
        """Return a non-required, failed ``Claude Code MCP`` CheckResult."""
        return CheckResult(
            name="Claude Code MCP", passed=False, message=message, required=False
        )


@final
class UserScopeShadow:
    """Prune a stray ``mcpServers.quarry`` entry from ``~/.claude.json``.

    ``quarry install`` once wrote a user-scope entry unconditionally
    (PL-999); the quarry Claude Code plugin now owns the server
    (tool-enable-disable.md §2.6), so that entry is pure shadow once the
    plugin is confirmed registered. A collaborator of :class:`ClaudeCodeMcp`
    rather than a method on it: it owns a disjoint piece of state (the
    ``~/.claude.json`` path) and a disjoint concern (pruning a stray write,
    not registering or checking the plugin-owned server).
    """

    __slots__ = ("_claude_json_path", "_plugin")

    _plugin: ClaudeCodeMcp
    _claude_json_path: Path

    def __new__(
        cls, plugin: ClaudeCodeMcp, claude_json_path: Path | None = None
    ) -> Self:
        self = super().__new__(cls)
        self._plugin = plugin
        self._claude_json_path = claude_json_path or (
            QuarryHome.home() / ".claude.json"
        )
        return self

    def prune(self) -> bool:
        """Remove the stray entry; a no-op, never an error, when not applicable.

        Opportunistic: a no-op when the plugin is absent (the ``--no-plugin``
        path still needs its own user-scope entry), the file is absent, or
        the ``quarry`` key is already gone -- the file is then never touched.
        Returns whether a write happened.
        """
        if not self._plugin.check().passed:
            return False
        with FileLock.shared(self._claude_json_path):
            return self._strip_entry()

    def _strip_entry(self) -> bool:
        """Delete the ``quarry`` key under a held lock; the file-touch boundary."""
        try:
            config = JsonFile.load(self._claude_json_path)
        except (OSError, ValueError):
            return False
        servers = config.get("mcpServers")
        if not isinstance(servers, dict) or _MCP_SERVER_NAME not in servers:
            return False
        del servers[_MCP_SERVER_NAME]
        AtomicFile(self._claude_json_path).replace(json.dumps(config, indent=2) + "\n")
        return True
