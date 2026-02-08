from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    message: str
    required: bool = True


def _check_python_version() -> CheckResult:
    v = sys.version_info
    version = f"{v.major}.{v.minor}.{v.micro}"
    return CheckResult(
        name="Python version",
        passed=True,
        message=version,
        required=False,
    )


def _check_data_directory() -> CheckResult:
    data_dir = Path.home() / ".quarry" / "data" / "lancedb"
    if data_dir.exists() and os.access(data_dir, os.W_OK):
        return CheckResult(
            name="Data directory",
            passed=True,
            message=str(data_dir),
        )
    if data_dir.exists():
        return CheckResult(
            name="Data directory",
            passed=False,
            message=f"{data_dir} exists but is not writable",
        )
    return CheckResult(
        name="Data directory",
        passed=False,
        message=f"{data_dir} does not exist (run 'quarry install')",
    )


def _check_aws_credentials() -> CheckResult:
    import botocore.session  # noqa: PLC0415

    session = botocore.session.get_session()
    try:
        credentials = session.get_credentials()
        resolved = credentials.get_frozen_credentials()
        key = resolved.access_key
    except Exception:  # noqa: BLE001
        return CheckResult(
            name="AWS credentials",
            passed=False,
            message="No credentials found (env vars, ~/.aws/credentials, or IAM role)",
        )
    if not key:
        return CheckResult(
            name="AWS credentials",
            passed=False,
            message="No credentials found (env vars, ~/.aws/credentials, or IAM role)",
        )
    masked = key[:4] + "****" + key[-4:]
    method = getattr(credentials, "method", "unknown")
    return CheckResult(
        name="AWS credentials",
        passed=True,
        message=f"{masked} (via {method})",
    )


def _check_embedding_model() -> CheckResult:
    model_dir = (
        Path.home()
        / ".cache"
        / "huggingface"
        / "hub"
        / "models--Snowflake--snowflake-arctic-embed-m-v1.5"
    )
    if model_dir.exists():
        return CheckResult(
            name="Embedding model",
            passed=True,
            message="snowflake-arctic-embed-m-v1.5 cached",
        )
    return CheckResult(
        name="Embedding model",
        passed=False,
        message="Not cached (run 'quarry install')",
    )


def _check_imports() -> CheckResult:
    modules = ["lancedb", "sentence_transformers", "fitz", "PIL", "boto3"]
    failed: list[str] = []
    for mod in modules:
        try:
            __import__(mod)
        except ImportError:
            failed.append(mod)
    if not failed:
        return CheckResult(
            name="Core imports",
            passed=True,
            message=f"{len(modules)} modules OK",
        )
    return CheckResult(
        name="Core imports",
        passed=False,
        message=f"Failed: {', '.join(failed)}",
    )


_MCP_SERVER_NAME = "quarry"
_MCP_COMMAND = "uvx"
_MCP_ARGS = ["quarry-mcp", "mcp"]

_DESKTOP_CONFIG_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Claude"
    / "claude_desktop_config.json"
)


def _configure_claude_code() -> CheckResult:
    """Add quarry MCP server to Claude Code via `claude mcp add`."""
    claude_path = shutil.which("claude")
    if claude_path is None:
        return CheckResult(
            name="Claude Code MCP",
            passed=False,
            message="claude CLI not found on PATH",
            required=False,
        )
    result = subprocess.run(  # noqa: S603
        [claude_path, "mcp", "add", _MCP_SERVER_NAME, "--", _MCP_COMMAND, *_MCP_ARGS],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "already exists" in stderr:
            return CheckResult(
                name="Claude Code MCP",
                passed=True,
                message="already configured",
            )
        return CheckResult(
            name="Claude Code MCP",
            passed=False,
            message=f"claude mcp add failed: {stderr}",
            required=False,
        )
    return CheckResult(
        name="Claude Code MCP",
        passed=True,
        message="configured (scope: local)",
    )


def _configure_claude_desktop() -> CheckResult:
    """Add quarry MCP server to Claude Desktop config."""
    config_path = _DESKTOP_CONFIG_PATH
    if not config_path.parent.exists():
        return CheckResult(
            name="Claude Desktop MCP",
            passed=False,
            message="Claude Desktop not installed",
            required=False,
        )

    server_entry = {"command": _MCP_COMMAND, "args": _MCP_ARGS}

    config = json.loads(config_path.read_text()) if config_path.exists() else {}

    mcp_servers: dict[str, object] = config.setdefault("mcpServers", {})
    mcp_servers[_MCP_SERVER_NAME] = server_entry
    config_path.write_text(json.dumps(config, indent=2) + "\n")

    return CheckResult(
        name="Claude Desktop MCP",
        passed=True,
        message=f"configured in {config_path.name} (restart Desktop to activate)",
    )


def run_install() -> int:
    """Create data directory, download model, and configure MCP clients.

    Returns 0 on success, 1 on failure.
    """
    data_dir = Path.home() / ".quarry" / "data" / "lancedb"

    print("Creating data directory...")  # noqa: T201
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"  \u2713 {data_dir}")  # noqa: T201

    print("Downloading embedding model...")  # noqa: T201
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415

    SentenceTransformer("Snowflake/snowflake-arctic-embed-m-v1.5")
    print("  \u2713 snowflake-arctic-embed-m-v1.5 cached")  # noqa: T201

    print("Configuring MCP clients...")  # noqa: T201
    for check in [_configure_claude_code(), _configure_claude_desktop()]:
        symbol = "\u2713" if check.passed else "\u25cb"
        print(f"  {symbol} {check.name}: {check.message}")  # noqa: T201

    return 0


def check_environment() -> int:
    """Run all environment checks. Returns 0 if all required pass, 1 otherwise."""
    checks = [
        _check_python_version(),
        _check_data_directory(),
        _check_aws_credentials(),
        _check_embedding_model(),
        _check_imports(),
    ]

    for check in checks:
        if check.passed:
            symbol = "\u2713"
        elif check.required:
            symbol = "\u2717"
        else:
            symbol = "\u25cb"
        print(f"  {symbol} {check.name}: {check.message}")  # noqa: T201

    required_failures = [c for c in checks if c.required and not c.passed]
    if required_failures:
        print(f"\n{len(required_failures)} issue(s) found.")  # noqa: T201
        return 1
    print("\nAll checks passed.")  # noqa: T201
    return 0
