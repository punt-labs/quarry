from __future__ import annotations

import os
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
    key_id = os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    if key_id and secret:
        masked = key_id[:4] + "****" + key_id[-4:]
        return CheckResult(
            name="AWS credentials",
            passed=True,
            message=f"AWS_ACCESS_KEY_ID={masked}",
        )
    missing = []
    if not key_id:
        missing.append("AWS_ACCESS_KEY_ID")
    if not secret:
        missing.append("AWS_SECRET_ACCESS_KEY")
    return CheckResult(
        name="AWS credentials",
        passed=False,
        message=f"Missing: {', '.join(missing)}",
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


_MCP_CONFIG = """\
Add to your MCP client configuration:

  {
    "quarry": {
      "command": "uvx",
      "args": ["quarry-mcp", "mcp"]
    }
  }
"""


def run_install() -> int:
    """Create data directory and download embedding model.

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

    print()  # noqa: T201
    print(_MCP_CONFIG)  # noqa: T201
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
