"""Environment diagnostics and install: verify deps, download model, configure."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    message: str
    required: bool = True


def _quarry_version() -> str:
    from importlib.metadata import version  # noqa: PLC0415

    return version("punt-quarry")


@contextlib.contextmanager
def _quiet_logging() -> Iterator[None]:
    """Temporarily suppress third-party logging during checks.

    RapidOCR adds its own StreamHandler that writes to stderr during init.
    Setting root logger level isn't enough — we must redirect stderr to
    suppress output from handlers created after our context enters.
    """
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.CRITICAL)
    devnull = open(os.devnull, "w")  # noqa: SIM115, PTH123
    old_stderr = sys.stderr
    sys.stderr = devnull
    try:
        yield
    finally:
        sys.stderr = old_stderr
        devnull.close()
        root.setLevel(previous_level)


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
    data_dir = Path.home() / ".quarry" / "data" / "default" / "lancedb"
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


def _check_embedding_model() -> CheckResult:
    from huggingface_hub import try_to_load_from_cache  # noqa: PLC0415

    from quarry.config import (  # noqa: PLC0415
        ONNX_MODEL_FILE,
        ONNX_MODEL_REPO,
        ONNX_MODEL_REVISION,
        ONNX_TOKENIZER_FILE,
    )

    model_cached = try_to_load_from_cache(
        ONNX_MODEL_REPO, ONNX_MODEL_FILE, revision=ONNX_MODEL_REVISION
    )
    tokenizer_cached = try_to_load_from_cache(
        ONNX_MODEL_REPO, ONNX_TOKENIZER_FILE, revision=ONNX_MODEL_REVISION
    )
    if (
        isinstance(model_cached, str)
        and Path(model_cached).exists()
        and isinstance(tokenizer_cached, str)
        and Path(tokenizer_cached).exists()
    ):
        model_size = Path(model_cached).stat().st_size
        return CheckResult(
            name="Embedding model",
            passed=True,
            message=(
                "snowflake-arctic-embed-m-v1.5 (ONNX INT8) cached"
                f" ({_human_size(model_size)})"
            ),
        )
    return CheckResult(
        name="Embedding model",
        passed=False,
        message="Not cached (run 'quarry install')",
    )


def _check_local_ocr() -> CheckResult:
    """Check that the local OCR engine (RapidOCR) can initialize."""
    try:
        from quarry.ocr_local import get_engine  # noqa: PLC0415

        get_engine()
        return CheckResult(
            name="Local OCR",
            passed=True,
            message="RapidOCR engine OK",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="Local OCR",
            passed=False,
            message=str(exc),
        )


def _check_imports() -> CheckResult:
    modules = [
        "lancedb",
        "tokenizers",
        "huggingface_hub",
        "fitz",
        "PIL",
        "rapidocr",
        "onnxruntime",
        "cv2",
    ]
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


def _check_storage() -> CheckResult:
    """Report database storage size."""
    data_dir = Path.home() / ".quarry" / "data"
    if not data_dir.exists():
        return CheckResult(
            name="Storage",
            passed=True,
            message="no data yet",
            required=False,
        )
    total = sum(f.stat().st_size for f in data_dir.rglob("*") if f.is_file())
    return CheckResult(
        name="Storage",
        passed=True,
        message=f"{_human_size(total)} in {data_dir}",
        required=False,
    )


def _human_size(nbytes: int) -> str:
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024 or unit == "TB":
            return f"{nbytes:.1f} {unit}" if nbytes >= 10 else f"{nbytes:.2f} {unit}"
        nbytes /= 1024  # type: ignore[assignment]
    return f"{nbytes:.1f} TB"  # unreachable but satisfies type checker


_MCP_SERVER_NAME = "quarry"


def _mcp_fallback_script(*, resolve_paths: bool = False) -> tuple[str, list[str]]:
    """Build ``sh -c`` command that prefers mcp-proxy, falls back to ``quarry mcp``.

    When *resolve_paths* is True (Claude Desktop), embeds shell-quoted
    absolute paths because Desktop runs with a minimal PATH.  The
    ``command -v`` check always uses bare names so the fallback works
    even if the binary is removed after install.
    """
    import shlex  # noqa: PLC0415

    from quarry.config import DEFAULT_PORT  # noqa: PLC0415

    ws_url = f"ws://localhost:{DEFAULT_PORT}/mcp"

    if resolve_paths:
        proxy_exec = shlex.quote(shutil.which("mcp-proxy") or "mcp-proxy")
        quarry_exec = shlex.quote(shutil.which("quarry") or "quarry")
        sh = shutil.which("sh") or "/bin/sh"
    else:
        proxy_exec = "mcp-proxy"
        quarry_exec = "quarry"
        sh = "sh"

    script = (
        "if command -v mcp-proxy >/dev/null 2>&1; "
        f"then exec {proxy_exec} {ws_url}; "
        f"else exec {quarry_exec} mcp; fi"
    )
    return sh, ["-c", script]


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
    command, args = _mcp_fallback_script()
    result = subprocess.run(  # noqa: S603
        [claude_path, "mcp", "add", _MCP_SERVER_NAME, "--", command, *args],
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

    command, args = _mcp_fallback_script(resolve_paths=True)
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


def _check_mcp_proxy() -> CheckResult:
    """Check whether mcp-proxy binary is installed and on PATH."""
    from quarry.proxy import installed_path  # noqa: PLC0415

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


def _check_claude_code_mcp() -> CheckResult:
    """Check whether quarry MCP is configured in Claude Code (read-only)."""
    claude_path = shutil.which("claude")
    if claude_path is None:
        return CheckResult(
            name="Claude Code MCP",
            passed=False,
            message="claude CLI not found on PATH",
            required=False,
        )
    try:
        result = subprocess.run(  # noqa: S603
            [claude_path, "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="Claude Code MCP",
            passed=False,
            message="claude CLI timed out",
            required=False,
        )
    if result.returncode != 0:
        return CheckResult(
            name="Claude Code MCP",
            passed=False,
            message="could not list MCP servers (run 'quarry install')",
            required=False,
        )
    if _MCP_SERVER_NAME in result.stdout:
        return CheckResult(
            name="Claude Code MCP",
            passed=True,
            message="configured",
        )
    return CheckResult(
        name="Claude Code MCP",
        passed=False,
        message="not configured (run 'quarry install')",
        required=False,
    )


def _check_claude_desktop_mcp() -> CheckResult:
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


def _print_check(check: CheckResult) -> None:
    """Print a single check result with appropriate symbol."""
    if check.passed:
        symbol = "\u2713"
    elif check.required:
        symbol = "\u2717"
    else:
        symbol = "\u25cb"
    print(f"  {symbol} {check.name}: {check.message}")  # noqa: T201


_QUARRY_CLAUDE_MD_SECTION = """\

<!-- quarry:capabilities -->
# Quarry

Local semantic search is available via quarry. Use it to search indexed
documents by meaning, ingest new content, and recall knowledge across sessions.

- **Slash commands**: `/find`, `/ingest`, `/remember`, `/explain`, `/source`,
  `/quarry`
- **Research agent**: `researcher` — combines quarry local search with web
  research. Use for deep investigation across local docs and the web.
- **Auto-behaviors**: working directory is auto-indexed at session start;
  URLs fetched via WebFetch are auto-ingested; transcripts are captured before
  context compaction.
- **Search tip**: natural language queries work best ("What were Q3 margins?"
  outperforms "Q3 margins").
<!-- /quarry:capabilities -->
"""

_QUARRY_SECTION_MARKER = "<!-- quarry:capabilities -->"


def _inject_claude_md() -> str:
    """Append a quarry capabilities section to ~/.claude/CLAUDE.md.

    Idempotent: skips if the section already exists.

    Returns:
        Status message for display.
    """
    claude_dir = Path.home() / ".claude"
    claude_md = claude_dir / "CLAUDE.md"

    claude_dir.mkdir(parents=True, exist_ok=True)

    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        if _QUARRY_SECTION_MARKER in content:
            return f"{claude_md} already has quarry section"
        with claude_md.open("a", encoding="utf-8") as f:
            f.write(_QUARRY_CLAUDE_MD_SECTION)
    else:
        claude_md.write_text(_QUARRY_CLAUDE_MD_SECTION.lstrip(), encoding="utf-8")

    return f"Appended quarry section to {claude_md}"


def run_install() -> int:
    """Create data directory, download model, and configure MCP clients.

    Returns 0 on success, 1 on failure.
    """
    print(f"punt-quarry {_quarry_version()}")  # noqa: T201
    print()  # noqa: T201

    failed = False

    # Step 1: data directory
    data_dir = Path.home() / ".quarry" / "data" / "default" / "lancedb"
    print("[1/6] Creating data directory...")  # noqa: T201
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        print(f"  \u2713 {data_dir}")  # noqa: T201
    except OSError as exc:
        print(f"  \u2717 Failed to create {data_dir}: {exc}")  # noqa: T201
        failed = True

    # Step 2: embedding model
    print("[2/6] Downloading embedding model...")  # noqa: T201
    try:
        from quarry.embeddings import download_model_files  # noqa: PLC0415

        download_model_files()
        print("  \u2713 snowflake-arctic-embed-m-v1.5 (INT8 ONNX) cached")  # noqa: T201
    except Exception as exc:  # noqa: BLE001
        print(f"  \u2717 Model download failed: {exc}")  # noqa: T201
        failed = True

    # Step 3: mcp-proxy binary (best-effort — proxy is optional, falls back to direct)
    # Installed before MCP client config so Desktop can resolve the absolute path.
    print("[3/6] Installing mcp-proxy...")  # noqa: T201
    try:
        from quarry.proxy import install as proxy_install  # noqa: PLC0415

        msg = proxy_install()
        print(f"  \u2713 {msg}")  # noqa: T201
    except Exception as exc:  # noqa: BLE001
        print(f"  \u2022 Skipped: {exc}")  # noqa: T201
        print("    mcp-proxy is optional — quarry works without it.")  # noqa: T201

    # Step 4: MCP clients (uses mcp-proxy if step 3 succeeded, otherwise quarry mcp)
    print("[4/6] Configuring MCP clients...")  # noqa: T201
    for check in [_configure_claude_code(), _configure_claude_desktop()]:
        _print_check(check)

    # Step 5: daemon service (best-effort — not available in CI, containers, SSH)
    print("[5/6] Registering quarry daemon...")  # noqa: T201
    try:
        from quarry.service import install as svc_install  # noqa: PLC0415

        msg = svc_install()
        print(f"  \u2713 {msg}")  # noqa: T201
    except Exception as exc:  # noqa: BLE001
        print(f"  \u2022 Skipped: {exc}")  # noqa: T201
        print("    Daemon registration is optional — quarry works without it.")  # noqa: T201

    # Step 6: CLAUDE.md context injection (best-effort)
    print("[6/6] Injecting quarry context into CLAUDE.md...")  # noqa: T201
    try:
        msg = _inject_claude_md()
        print(f"  \u2713 {msg}")  # noqa: T201
    except Exception as exc:  # noqa: BLE001
        print(f"  \u2022 Skipped: {exc}")  # noqa: T201

    # Verification
    print()  # noqa: T201
    print("Verifying installation...")  # noqa: T201
    exit_code = check_environment(_skip_header=True)
    if failed:
        return 1
    return exit_code


def check_environment(*, _skip_header: bool = False) -> int:
    """Run all environment checks. Returns 0 if all required pass, 1 otherwise."""
    if not _skip_header:
        print(f"punt-quarry {_quarry_version()}")  # noqa: T201
        print()  # noqa: T201

    with _quiet_logging():
        all_results: list[CheckResult | None] = [
            _check_python_version(),
            _check_data_directory(),
            _check_local_ocr(),
            _check_embedding_model(),
            _check_imports(),
            _check_mcp_proxy(),
            _check_claude_code_mcp(),
            _check_claude_desktop_mcp(),
            _check_storage(),
        ]
        checks: list[CheckResult] = [c for c in all_results if c is not None]

    for check in checks:
        _print_check(check)

    required_failures = [c for c in checks if c.required and not c.passed]
    if required_failures:
        print(f"\n{len(required_failures)} issue(s) found.")  # noqa: T201
        return 1
    print("\nAll checks passed.")  # noqa: T201
    return 0
