"""Claude Desktop's wiring of the quarry MCP server: write it, and check it."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Self, final

from quarry.atomic_file import AtomicFile
from quarry.home_paths import QuarryHome
from quarry.json_file import ConfigError, JsonFile
from quarry.mcp_constants import MCP_SERVER_NAME as _MCP_SERVER_NAME
from quarry.results import CheckResult

__all__ = ["ClaudeDesktopMcp"]


@final
class ClaudeDesktopMcp:
    """Claude Desktop's wiring of the quarry MCP server: write it, and check it."""

    __slots__ = ("_config_path",)

    _config_path: Path

    def __new__(cls, config_path: Path | None = None) -> Self:
        self = super().__new__(cls)
        self._config_path = config_path or (
            QuarryHome.home()
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
        except (OSError, ValueError, RecursionError) as exc:
            return self._failure(f"{JsonFile.why(exc, path)}; left unchanged")
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
            return self._failure(f"config error: {JsonFile.why(exc, path)}")
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
        first = shutil.which("quarry")
        if first is None:
            msg = "quarry is not on PATH, so Claude Desktop cannot be pointed at it"
            raise ConfigError(msg)
        if Path(first).is_absolute():
            return first  # symlinks are kept: a uv tool shim is a legitimate one
        # A relative PATH entry won: look past it for an absolute match.
        absolute = os.pathsep.join(
            entry
            for entry in os.environ.get("PATH", "").split(os.pathsep)
            if Path(entry).is_absolute()
        )
        found = shutil.which("quarry", path=absolute)
        if found is None:
            msg = (
                f"PATH has a relative entry; quarry resolved to {first!r} and no "
                "absolute PATH entry has it (put quarry's directory on PATH by its "
                "full path)"
            )
            raise ConfigError(msg)
        # Symlinks are kept: a uv tool shim is a legitimate one.
        return found

    @staticmethod
    def _failure(message: str) -> CheckResult:
        """Return a non-required, failed ``Claude Desktop MCP`` CheckResult."""
        return CheckResult(
            name="Claude Desktop MCP", passed=False, message=message, required=False
        )
