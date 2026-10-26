"""MCP client wiring: configure Claude Code and Desktop, and check they are."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from functools import partial
from pathlib import Path
from typing import ClassVar, Self, final

from quarry.atomic_file import AtomicFile
from quarry.doctor_failure import CheckFailure
from quarry.json_file import ConfigError, JsonFile
from quarry.results import CheckResult

_MCP_SERVER_NAME = "quarry"
_SCOPE_USER = ("--scope", "user")


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
            Path.home() / ".claude" / "plugins" / "installed_plugins.json"
        )
        return self

    def configure(self) -> CheckResult:
        """Register the quarry MCP server with Claude Code, replacing any stale entry.

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
        exceeds the timeout when many plugins are installed.

        NOTE: This check reads the plugin registry (installed_plugins.json),
        which is populated by ``claude plugin install``.  The write path in
        :meth:`configure` uses ``claude mcp add``, which writes to a different
        store.  In practice these are in sync because quarry is always
        installed as a plugin via the install scripts.  If a user runs
        ``quarry install`` standalone (without the plugin), this check may
        report "not configured" even though the MCP server was added.  This is
        acceptable for a ``required=False`` diagnostic check.
        """
        try:
            if not self._plugins_path.exists():
                return self._failure("no plugin registry found")
            return self._read_registry()
        except (OSError, ValueError) as exc:
            return self._failure(f"config error: {JsonFile.why(exc)}")

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
        raw_path = entries[0].get("installPath", "")
        if not raw_path or not isinstance(raw_path, str):
            return self._failure("plugin registry has empty installPath")
        install_path = Path(raw_path)
        plugin_json = install_path / ".claude-plugin" / "plugin.json"
        if not plugin_json.exists():
            return self._failure(f"plugin files missing at {install_path.name}")
        manifest = JsonFile.load(plugin_json)
        servers = JsonFile.object_at(manifest, "mcpServers", plugin_json)
        if _MCP_SERVER_NAME not in servers:
            return self._failure("plugin manifest missing quarry MCP server entry")
        return CheckResult(name="Claude Code MCP", passed=True, message="configured")

    def _register(self, claude_path: str) -> CheckResult:
        """Add the quarry server; remove and re-add a stale entry that blocks it."""
        ok = CheckResult(
            name="Claude Code MCP", passed=True, message="configured (scope: user)"
        )
        deadline = time.monotonic() + self._TOTAL_TIMEOUT_S
        run = partial(self._run, claude_path, deadline)
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
            return ok
        if "already exists" not in result.stderr:
            return self._failure(f"claude mcp add failed: {self._said(result)}")
        # An entry already exists and blocks the direct add. Remove it and re-add;
        # only now — with the entry confirmed present — do we risk a removal.
        remove = run("mcp", "remove", _MCP_SERVER_NAME, *_SCOPE_USER)
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
            return ok
        return self._failure(self._readd_failed(self._said(retry)))

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
        """Return what ``claude`` said on stderr: one printable, bounded line."""
        return CheckFailure.line(result.stderr)

    @classmethod
    def _run(
        cls, claude_path: str, deadline: float, *argv: str
    ) -> subprocess.CompletedProcess[str]:
        """Run the ``claude`` CLI with *argv* before *deadline* (the one S603 site).

        Its output is decoded as UTF-8 with replacement, so bytes that are not
        text cannot raise.
        """
        timeout = min(cls._CLAUDE_TIMEOUT_S, deadline - time.monotonic())
        if timeout <= 0:
            raise subprocess.TimeoutExpired([claude_path, *argv], 0)
        return subprocess.run(  # noqa: S603
            [claude_path, *argv],
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
class ClaudeDesktopMcp:
    """Claude Desktop's wiring of the quarry MCP server: write it, and check it."""

    __slots__ = ("_config_path",)

    _config_path: Path

    def __new__(cls, config_path: Path | None = None) -> Self:
        self = super().__new__(cls)
        self._config_path = config_path or (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
        return self

    def configure(self) -> CheckResult:
        """Add quarry MCP server to Claude Desktop config; always return a result.

        Uses an absolute path for the command since Desktop has a limited PATH.
        A config that is unreadable, not UTF-8, not JSON, not an object, or not a
        regular file is reported and left exactly as it was, so the install goes
        on to its next step.
        """
        path = self._config_path
        try:
            if not path.parent.exists():
                return self._failure("Claude Desktop not installed")
            config = JsonFile.load(path) if path.exists() else {}
            servers = JsonFile.object_at(config, "mcpServers", path)
            servers[_MCP_SERVER_NAME] = {"command": self._command(), "args": ["mcp"]}
            config["mcpServers"] = servers
            AtomicFile(path).replace(json.dumps(config, indent=2) + "\n")
        except (OSError, ValueError) as exc:
            return self._failure(f"{JsonFile.why(exc)}; left unchanged")
        return CheckResult(
            name="Claude Desktop MCP",
            passed=True,
            message=f"configured in {path.name} (restart Desktop to activate)",
        )

    def check(self) -> CheckResult:
        """Check whether quarry MCP is configured in Claude Desktop (read-only)."""
        path = self._config_path
        try:
            if not path.parent.exists():
                return self._failure("Claude Desktop not installed")
            if not path.exists():
                return self._failure("no config file (run 'quarry install')")
            servers = JsonFile.object_at(JsonFile.load(path), "mcpServers", path)
        except (OSError, ValueError) as exc:
            return self._failure(f"config error: {JsonFile.why(exc)}")
        if _MCP_SERVER_NAME in servers:
            return CheckResult(
                name="Claude Desktop MCP", passed=True, message="configured"
            )
        return self._failure("not configured (run 'quarry install')")

    @staticmethod
    def _command() -> str:
        """Return the absolute path of ``quarry`` (Desktop runs with a minimal PATH).

        Desktop cannot find a bare ``quarry``, so one that does not resolve is an
        error, never a registration that only looks configured.
        """
        found = shutil.which("quarry")
        if found is None:
            msg = "quarry is not on PATH, so Claude Desktop cannot be pointed at it"
            raise ConfigError(msg)
        return found

    @staticmethod
    def _failure(message: str) -> CheckResult:
        """Return a non-required, failed ``Claude Desktop MCP`` CheckResult."""
        return CheckResult(
            name="Claude Desktop MCP", passed=False, message=message, required=False
        )
