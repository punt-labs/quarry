"""Small, explicit live and real-agent contracts."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
from typing import TYPE_CHECKING

import httpx
import pytest

from quarry.http_url import HttpUrl
from quarry.ingestion.web_fetch import WebFetcher
from tests.hook_fixtures import ephemeral_daemon

if TYPE_CHECKING:
    from pathlib import Path

    from tests.hook_fixtures import EphemeralDaemon

__all__ = ["ephemeral_daemon"]


@pytest.mark.live
@pytest.mark.network
def test_fetch_public_https_page() -> None:
    """Exercise real DNS, certificate validation, and HTML decoding."""
    fetched = WebFetcher(timeout=10).fetch_body(HttpUrl("https://example.com/"))
    assert fetched.media_type == "text/html"
    assert "Example Domain" in fetched.text


@pytest.mark.agent
@pytest.mark.network
@pytest.mark.timeout(120)
def test_claude_recalls_quarry_canary(
    ephemeral_daemon: EphemeralDaemon, tmp_path: Path
) -> None:
    """Claude can only learn the unprompted random value from Quarry's MCP find."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    claude = shutil.which("claude")
    quarry = shutil.which("quarry")
    if not (api_key and claude and quarry):
        pytest.fail("Agent smoke needs ANTHROPIC_API_KEY, Claude Code, and Quarry")
    nonce = secrets.token_hex(12)
    _seed_recall_canary(ephemeral_daemon, nonce)
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "quarry": {
                        "command": "env",
                        "args": ["-u", "ANTHROPIC_API_KEY", quarry, "mcp"],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    run = subprocess.run(
        [
            claude,
            "--print",
            "--model",
            "haiku",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp_config),
            "--tools",
            "",
            "--allowedTools",
            "mcp__quarry__find",
            "--max-turns",
            "3",
            "--max-budget-usd",
            "0.05",
            "--no-session-persistence",
            "--output-format",
            "json",
        ],
        input=(
            "Call mcp__quarry__find in collection qa-agent, with query "
            "'nightly audit passphrase'. Return only the passphrase from its results."
        ),
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
            "ANTHROPIC_API_KEY": api_key,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "ENABLE_TOOL_SEARCH": "false",
            "QUARRY_ROOT": str(ephemeral_daemon.data_dir),
            "QUARRY_URL": ephemeral_daemon.base_url,
            "QUARRY_TOKEN": ephemeral_daemon.api_token,
            "PUNT_TELEMETRY": "off",
        },
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if run.returncode != 0:
        pytest.fail(
            f"Claude Code exited {run.returncode}; check scoped key and model access"
        )
    try:
        result = json.loads(run.stdout)
    except json.JSONDecodeError:
        pytest.fail("Claude Code did not emit a JSON result", pytrace=False)
    if not isinstance(result, dict) or result.get("is_error") is not False:
        pytest.fail("Claude Code reported an unsuccessful run", pytrace=False)
    answer = result.get("result")
    if not isinstance(answer, str) or answer.strip() != nonce:
        pytest.fail("Claude Code did not return the Quarry canary")


def _seed_recall_canary(daemon: EphemeralDaemon, nonce: str) -> None:
    with httpx.Client(
        base_url=daemon.base_url,
        headers={"Authorization": f"Bearer {daemon.api_token}"},
        timeout=2,
        trust_env=False,
    ) as client:
        response = client.post(
            "/v1/remember",
            json={
                "name": "agent-smoke",
                "content": f"The nightly audit passphrase is {nonce}",
                "collection": "qa-agent",
            },
        )
        response.raise_for_status()
        task_id = response.json()["task_id"]
        deadline = time.monotonic() + 30
        status = ""
        while time.monotonic() < deadline:
            response = client.get(f"/v1/tasks/{task_id}")
            response.raise_for_status()
            status = response.json()["status"]
            if status in {"completed", "failed"}:
                break
            time.sleep(0.25)
        assert status == "completed", "The synthetic canary was not indexed"
