"""MCP client wiring: configure Claude Code and Desktop, and check they are."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from quarry.proxy import installed_path
from quarry.results import CheckResult

_MCP_SERVER_NAME = "quarry"
_SCOPE_USER = ("--scope", "user")


def mcp_command(*, resolve_paths: bool = False) -> tuple[str, list[str]]:
    """Return the command that runs the quarry MCP server directly.

    ``quarry mcp`` is a stdio FastMCP client of the daemon: the
    server reaches quarryd through ``QuarryClient``, so there is no mcp-proxy
    shim and no ``sh -c`` wrapper.  Remote access is carried by the client's own
    TLS + pinned-CA login config, not a proxy.

    When *resolve_paths* is True (Claude Desktop runs with a minimal PATH) the
    ``quarry`` binary is resolved to an absolute path.
    """
    quarry_exec = shutil.which("quarry") or "quarry" if resolve_paths else "quarry"
    return quarry_exec, ["mcp"]


_DESKTOP_CONFIG_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Claude"
    / "claude_desktop_config.json"
)


def _run_claude(claude_path: str, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run the ``claude`` CLI with *argv*, capturing output (the one S603 site)."""
    return subprocess.run(  # noqa: S603
        [claude_path, *argv], capture_output=True, text=True
    )


def _claude_code_failure(message: str) -> CheckResult:
    """Return a non-required, failed ``Claude Code MCP`` CheckResult."""
    return CheckResult(
        name="Claude Code MCP", passed=False, message=message, required=False
    )


def configure_claude_code() -> CheckResult:
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
    """
    ok = CheckResult(
        name="Claude Code MCP", passed=True, message="configured (scope: user)"
    )
    claude_path = shutil.which("claude")
    if claude_path is None:
        return _claude_code_failure("claude CLI not found on PATH")
    command, args = mcp_command()
    add_argv = ["mcp", "add", _MCP_SERVER_NAME, *_SCOPE_USER, "--", command, *args]
    result = _run_claude(claude_path, *add_argv)
    if result.returncode == 0:
        return ok
    if "already exists" not in result.stderr:
        return _claude_code_failure(f"claude mcp add failed: {result.stderr.strip()}")
    # An entry already exists and blocks the direct add. Remove it and re-add;
    # only now — with the entry confirmed present — do we risk a removal.
    remove = _run_claude(claude_path, "mcp", "remove", _MCP_SERVER_NAME, *_SCOPE_USER)
    if remove.returncode != 0:
        # The remove failed: the stale entry is likely still present, so do NOT
        # re-add blindly or claim a removal that did not happen.
        return _claude_code_failure(
            "a stale quarry MCP entry blocks the add but could not be removed: "
            f"{remove.stderr.strip()}. Inspect with 'claude mcp list' — a stale "
            "'local'-scope entry can shadow the 'user'-scope one and cause this "
            "exact failure; try 'claude mcp remove quarry --scope local' first, "
            "then re-run 'quarry install'."
        )
    retry = _run_claude(claude_path, *add_argv)
    if retry.returncode == 0:
        return ok
    return _claude_code_failure(
        "removed the stale quarry MCP entry but the re-add failed: "
        f"{retry.stderr.strip()}. Re-run 'quarry install' or "
        "'claude mcp add quarry --scope user -- quarry mcp'."
    )


def configure_claude_desktop() -> CheckResult:
    """Add quarry MCP server to Claude Desktop config.

    Uses absolute path for the command since Desktop has a limited PATH.
    """
    config_path = _DESKTOP_CONFIG_PATH
    if not config_path.parent.exists():
        return CheckResult(
            name="Claude Desktop MCP",
            passed=False,
            message="Claude Desktop not installed",
            required=False,
        )

    command, args = mcp_command(resolve_paths=True)
    server_entry = {"command": command, "args": args}

    config = json.loads(config_path.read_text()) if config_path.exists() else {}

    mcp_servers: dict[str, object] = config.setdefault("mcpServers", {})
    mcp_servers[_MCP_SERVER_NAME] = server_entry
    config_path.write_text(json.dumps(config, indent=2) + "\n")

    return CheckResult(
        name="Claude Desktop MCP",
        passed=True,
        message=f"configured in {config_path.name} (restart Desktop to activate)",
    )


def check_mcp_proxy() -> CheckResult:
    """Check whether mcp-proxy binary is installed and on PATH."""
    path = installed_path()
    if path:
        return CheckResult(
            name="mcp-proxy",
            passed=True,
            message=f"found at {path}",
            required=False,
        )
    return CheckResult(
        name="mcp-proxy",
        passed=False,
        message="not found on PATH (run 'quarry install')",
        required=False,
    )


_CLAUDE_CODE_PLUGINS_PATH = (
    Path.home() / ".claude" / "plugins" / "installed_plugins.json"
)

_QUARRY_PLUGIN_KEY = "quarry@punt-labs"


def check_claude_code_mcp() -> CheckResult:
    """Check whether quarry MCP is configured in Claude Code (read-only).

    Reads the plugin registry JSON directly instead of shelling out to
    ``claude mcp list``, which spawns every configured MCP server and
    exceeds the timeout when many plugins are installed.

    NOTE: This check reads the plugin registry (installed_plugins.json),
    which is populated by ``claude plugin install``.  The write path in
    ``configure_claude_code()`` uses ``claude mcp add``, which writes to a
    different store.  In practice these are in sync because quarry is
    always installed as a plugin via the install scripts.  If a user
    runs ``quarry install`` standalone (without the plugin), this check
    may report "not configured" even though the MCP server was added.
    This is acceptable for a ``required=False`` diagnostic check.
    """
    plugins_path = _CLAUDE_CODE_PLUGINS_PATH
    if not plugins_path.exists():
        return _claude_code_failure("no plugin registry found")
    try:
        data = json.loads(plugins_path.read_text(encoding="utf-8"))
        plugins = data.get("plugins", {})
        if _QUARRY_PLUGIN_KEY not in plugins:
            return _claude_code_failure("not configured (run 'quarry install')")
        # Verify the install path contains a valid plugin manifest with
        # an mcpServers entry for quarry.  This catches stale registry
        # entries where the plugin directory was deleted or corrupted.
        entries = plugins[_QUARRY_PLUGIN_KEY]
        if not entries:
            return _claude_code_failure("not configured (run 'quarry install')")
        raw_path = entries[0].get("installPath", "")
        if not raw_path:
            return _claude_code_failure("plugin registry has empty installPath")
        install_path = Path(raw_path)
        plugin_json = install_path / ".claude-plugin" / "plugin.json"
        if not plugin_json.exists():
            return _claude_code_failure(f"plugin files missing at {install_path}")
        manifest = json.loads(plugin_json.read_text(encoding="utf-8"))
        if _MCP_SERVER_NAME not in manifest.get("mcpServers", {}):
            return _claude_code_failure(
                "plugin manifest missing quarry MCP server entry"
            )
        return CheckResult(
            name="Claude Code MCP",
            passed=True,
            message="configured",
        )
    except (json.JSONDecodeError, OSError, KeyError, TypeError, AttributeError) as exc:
        return _claude_code_failure(f"config error: {exc}")


def check_claude_desktop_mcp() -> CheckResult:
    """Check whether quarry MCP is configured in Claude Desktop (read-only)."""
    config_path = _DESKTOP_CONFIG_PATH
    if not config_path.parent.exists():
        return CheckResult(
            name="Claude Desktop MCP",
            passed=False,
            message="Claude Desktop not installed",
            required=False,
        )
    if not config_path.exists():
        return CheckResult(
            name="Claude Desktop MCP",
            passed=False,
            message="no config file (run 'quarry install')",
            required=False,
        )
    try:
        config = json.loads(config_path.read_text())
        servers = config.get("mcpServers", {})
        if _MCP_SERVER_NAME in servers:
            return CheckResult(
                name="Claude Desktop MCP",
                passed=True,
                message="configured",
            )
        return CheckResult(
            name="Claude Desktop MCP",
            passed=False,
            message="not configured (run 'quarry install')",
            required=False,
        )
    except (json.JSONDecodeError, OSError) as exc:
        return CheckResult(
            name="Claude Desktop MCP",
            passed=False,
            message=f"config error: {exc}",
            required=False,
        )
