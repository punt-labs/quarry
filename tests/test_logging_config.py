"""Check real process diagnostics and detached dispatcher outcome breadcrumbs."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _run(home: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *argv],
        env={**os.environ, "HOME": str(home), "QUARRY_LOG_LEVEL": "DEBUG"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


@pytest.mark.parametrize(
    ("args", "role"),
    [
        (("-m", "quarry", "version"), "cli"),
        (
            (
                "-c",
                "from quarry.daemon.launcher import entrypoint; entrypoint()",
                "--help",
            ),
            "daemon",
        ),
        (("-m", "quarry", "mcp"), "mcp"),
        (("-m", "quarry._hook_entry", "session-setup"), "hook"),
    ],
)
def test_entry_point_configures_shared_role(
    tmp_path: Path, args: tuple[str, ...], role: str
) -> None:
    """All public process surfaces select their role and open a private sink."""
    run = _run(tmp_path, *args)
    assert run.returncode == 0, run.stderr
    assert f"Configured role={role} pid=" in run.stderr
    path = tmp_path / ".punt-labs" / "quarry" / "logs" / "quarry.log"
    assert path.stat().st_mode & 0o777 == 0o600
    if role == "mcp":
        assert run.stdout == ""


@pytest.mark.parametrize(("code", "outcome"), [(0, "finished"), (1, "failed")])
def test_detached_dispatch_logs_exit_status(
    tmp_path: Path, code: int, outcome: str
) -> None:
    """Worker outcomes remain visible even when the launcher discards stderr."""
    script = (
        "from unittest.mock import patch\n"
        "from quarry.sync_worker import SyncWorker\n"
        "with patch('quarry.__main__.app', side_effect=SystemExit("
        + str(code)
        + ")):\n"
        "    SyncWorker.main()\n"
    )
    run = _run(tmp_path, "-c", script)
    assert run.returncode == code, run.stderr
    assert "Configured role=worker pid=" in run.stderr
    log = tmp_path / ".punt-labs" / "quarry" / "logs" / "quarry.log"
    assert "Background sync dispatcher started" in log.read_text()
    assert f"Background sync dispatcher {outcome}" in log.read_text()


def test_hook_breadcrumb_has_one_logger_prefix_and_intact_unknown(
    tmp_path: Path,
) -> None:
    script = (
        "import logging\n"
        "from quarry.logging_config import LoggingConfig\n"
        "from quarry._hook_trace import HookTrace\n"
        "LoggingConfig.hook()\n"
        "for handler in logging.getLogger().handlers:\n"
        "    handler.formatter.formatTime = lambda *args: '2000-01-01 00:00:00'\n"
        "trace = HookTrace('pre-compact')\n"
        "trace.mark_config(on=False)\n"
        "trace.skip('config')\n"
    )
    run = _run(tmp_path, "-c", script)
    assert run.returncode == 0, run.stderr
    path = tmp_path / ".punt-labs/quarry/logs/quarry.log"
    assert path.read_text() == (
        "2000-01-01 00:00:00 [INFO] quarry.hooks: pre-compact: "
        "entered (config=off, payload_ok=unknown) -> skip:config\n"
    )
