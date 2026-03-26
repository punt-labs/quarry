# ruff: noqa: S603, S607 — all subprocess calls invoke system binaries (launchctl, systemctl, loginctl)
"""Daemon lifecycle management for ``quarry serve``.

Provides ``install`` and ``uninstall`` commands that register quarry as a
system service (launchd on macOS, systemd on Linux) so the daemon starts
at login and restarts on crash.

The service runs ``quarry serve --port 8420`` using the Python interpreter
that executed the install command, anchoring to the exact venv/installation.
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

from quarry.config import DEFAULT_PORT

logger = logging.getLogger(__name__)

_LABEL = "com.punt-labs.quarry"


def _quarry_exec_args() -> list[str]:
    """Return the command to invoke ``quarry serve`` from the current Python."""
    return [sys.executable, "-m", "quarry", "serve", "--port", str(DEFAULT_PORT)]


# ---------------------------------------------------------------------------
# macOS — launchd
# ---------------------------------------------------------------------------

_LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
_LAUNCHD_PLIST = _LAUNCHD_DIR / f"{_LABEL}.plist"


def _launchd_plist_content() -> str:
    args = _quarry_exec_args()
    program_args = "\n".join(f"        <string>{shlex.quote(a)}</string>" for a in args)
    log_dir = Path.home() / ".punt-labs" / "quarry" / "logs"
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{_LABEL}</string>
            <key>ProgramArguments</key>
            <array>
        {program_args}
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>StandardOutPath</key>
            <string>{log_dir}/quarry-stdout.log</string>
            <key>StandardErrorPath</key>
            <string>{log_dir}/quarry-stderr.log</string>
        </dict>
        </plist>
    """)


def _launchd_install() -> None:
    _LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)

    # Unload any existing service first — handles upgrades where the
    # old plist pointed to a different binary (e.g. editable install).
    # Without this, `launchctl load` fails silently or with I/O error
    # when the label is already registered, and the old binary keeps
    # respawning via KeepAlive.
    if _launchd_status():
        result = subprocess.run(
            ["launchctl", "unload", "-w", str(_LAUNCHD_PLIST)],
            check=False,
        )
        if result.returncode == 0:
            logger.info("Unloaded existing %s before upgrade", _LABEL)
        else:
            logger.warning(
                "Could not unload %s (rc=%d) — proceeding with load",
                _LABEL,
                result.returncode,
            )

    _LAUNCHD_PLIST.write_text(_launchd_plist_content())
    logger.info("Wrote %s", _LAUNCHD_PLIST)

    subprocess.run(
        ["launchctl", "load", "-w", str(_LAUNCHD_PLIST)],
        check=True,
    )
    logger.info("Loaded %s into launchd", _LABEL)


def _launchd_uninstall() -> None:
    if _LAUNCHD_PLIST.exists():
        subprocess.run(
            ["launchctl", "unload", "-w", str(_LAUNCHD_PLIST)],
            check=False,  # may already be unloaded
        )
        _LAUNCHD_PLIST.unlink()
        logger.info("Removed %s", _LAUNCHD_PLIST)
    else:
        logger.info("No plist found at %s — nothing to uninstall", _LAUNCHD_PLIST)


def _launchd_status() -> bool:
    result = subprocess.run(
        ["launchctl", "list", _LABEL],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Linux — systemd user unit
# ---------------------------------------------------------------------------

_SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
_SYSTEMD_UNIT = _SYSTEMD_DIR / "quarry.service"


def _systemd_unit_content() -> str:
    args = _quarry_exec_args()
    exec_start = " ".join(shlex.quote(a) for a in args)
    return textwrap.dedent(f"""\
        [Unit]
        Description=Quarry semantic search daemon
        After=network.target

        [Service]
        ExecStart={exec_start}
        Restart=on-failure
        RestartSec=5

        [Install]
        WantedBy=default.target
    """)


def _systemd_install() -> None:
    _SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    _SYSTEMD_UNIT.write_text(_systemd_unit_content())
    logger.info("Wrote %s", _SYSTEMD_UNIT)

    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        check=True,
    )
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", "quarry"],
        check=True,
    )
    logger.info("Enabled and started quarry.service")


def _systemd_uninstall() -> None:
    if _SYSTEMD_UNIT.exists():
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "quarry"],
            check=False,  # may already be stopped
        )
        _SYSTEMD_UNIT.unlink()
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
        )
        logger.info("Removed %s", _SYSTEMD_UNIT)
    else:
        logger.info("No unit found at %s — nothing to uninstall", _SYSTEMD_UNIT)


def _systemd_status() -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "quarry"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "active"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_platform() -> str:
    """Return ``'macos'`` or ``'linux'``.  Raises on unsupported platforms."""
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Linux":
        return "linux"
    msg = f"Unsupported platform: {system}. quarry install supports macOS and Linux."
    raise SystemExit(msg)


def install() -> str:
    """Install quarry as a system service.  Returns a status message."""
    plat = detect_platform()
    args = _quarry_exec_args()

    if plat == "macos":
        _launchd_install()
        running = _launchd_status()
    else:
        _systemd_install()
        running = _systemd_status()

    exec_display = " ".join(args)
    status = "running" if running else "installed (not yet running)"
    lines = [
        f"quarry daemon {status} on port {DEFAULT_PORT}.",
        f"  Service: {_LAUNCHD_PLIST if plat == 'macos' else _SYSTEMD_UNIT}",
        f"  Command: {exec_display}",
    ]
    if plat == "linux" and not _has_linger():
        lines.append(
            "  Warning: loginctl linger is not enabled. "
            "The daemon will stop when you log out. "
            "Run: loginctl enable-linger"
        )
    return os.linesep.join(lines)


def uninstall() -> str:
    """Remove quarry system service.  Returns a status message."""
    plat = detect_platform()
    if plat == "macos":
        _launchd_uninstall()
        path = _LAUNCHD_PLIST
    else:
        _systemd_uninstall()
        path = _SYSTEMD_UNIT
    return f"quarry daemon uninstalled. Removed {path}."


def _has_linger() -> bool:
    """Check if loginctl linger is enabled for the current user (Linux only)."""
    result = subprocess.run(
        ["loginctl", "show-user", os.getlogin(), "--property=Linger"],
        capture_output=True,
        text=True,
    )
    return "Linger=yes" in result.stdout
