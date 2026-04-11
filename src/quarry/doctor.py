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
    data_dir = Path.home() / ".punt-labs" / "quarry" / "data" / "default" / "lancedb"
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
        ONNX_MODEL_REPO,
        ONNX_MODEL_REVISION,
        ONNX_TOKENIZER_FILE,
    )

    model_cached = try_to_load_from_cache(
        ONNX_MODEL_REPO,
        "onnx/model_int8.onnx",
        revision=ONNX_MODEL_REVISION,
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


def _check_provider() -> CheckResult:
    """Report which ONNX execution provider is selected."""
    from quarry.provider import select_provider  # noqa: PLC0415

    try:
        selection = select_provider()
        return CheckResult(
            name="ONNX provider",
            passed=True,
            message=f"{selection.provider} ({selection.model_file})",
            required=False,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="ONNX provider",
            passed=False,
            message=str(exc),
            required=False,
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
    data_dir = Path.home() / ".punt-labs" / "quarry" / "data"
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


_CLAUDE_CODE_PLUGINS_PATH = (
    Path.home() / ".claude" / "plugins" / "installed_plugins.json"
)

_QUARRY_PLUGIN_KEY = "quarry@punt-labs"


def _check_claude_code_mcp() -> CheckResult:
    """Check whether quarry MCP is configured in Claude Code (read-only).

    Reads the plugin registry JSON directly instead of shelling out to
    ``claude mcp list``, which spawns every configured MCP server and
    exceeds the timeout when many plugins are installed.

    NOTE: This check reads the plugin registry (installed_plugins.json),
    which is populated by ``claude plugin install``.  The write path in
    ``_configure_claude_code()`` uses ``claude mcp add``, which writes to a
    different store.  In practice these are in sync because quarry is
    always installed as a plugin via the install scripts.  If a user
    runs ``quarry install`` standalone (without the plugin), this check
    may report "not configured" even though the MCP server was added.
    This is acceptable for a ``required=False`` diagnostic check.
    """
    plugins_path = _CLAUDE_CODE_PLUGINS_PATH
    if not plugins_path.exists():
        return CheckResult(
            name="Claude Code MCP",
            passed=False,
            message="no plugin registry found",
            required=False,
        )
    try:
        data = json.loads(plugins_path.read_text(encoding="utf-8"))
        plugins = data.get("plugins", {})
        if _QUARRY_PLUGIN_KEY not in plugins:
            return CheckResult(
                name="Claude Code MCP",
                passed=False,
                message="not configured (run 'quarry install')",
                required=False,
            )
        # Verify the install path contains a valid plugin manifest with
        # an mcpServers entry for quarry.  This catches stale registry
        # entries where the plugin directory was deleted or corrupted.
        entries = plugins[_QUARRY_PLUGIN_KEY]
        if not entries:
            return CheckResult(
                name="Claude Code MCP",
                passed=False,
                message="not configured (run 'quarry install')",
                required=False,
            )
        raw_path = entries[0].get("installPath", "")
        if not raw_path:
            return CheckResult(
                name="Claude Code MCP",
                passed=False,
                message="plugin registry has empty installPath",
                required=False,
            )
        install_path = Path(raw_path)
        plugin_json = install_path / ".claude-plugin" / "plugin.json"
        if not plugin_json.exists():
            return CheckResult(
                name="Claude Code MCP",
                passed=False,
                message=f"plugin files missing at {install_path}",
                required=False,
            )
        manifest = json.loads(plugin_json.read_text(encoding="utf-8"))
        if _MCP_SERVER_NAME not in manifest.get("mcpServers", {}):
            return CheckResult(
                name="Claude Code MCP",
                passed=False,
                message="plugin manifest missing quarry MCP server entry",
                required=False,
            )
        return CheckResult(
            name="Claude Code MCP",
            passed=True,
            message="configured",
        )
    except (json.JSONDecodeError, OSError, KeyError, TypeError, AttributeError) as exc:
        return CheckResult(
            name="Claude Code MCP",
            passed=False,
            message=f"config error: {exc}",
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


_SESSION_CONTEXT_TEMPLATE = """\
## Memory

You have persistent memory stored in quarry, a local semantic
search engine. Your memories survive across sessions and machines.

### Working Memory

Collection: "{memory_collection}"

To recall prior knowledge:
  /find <query> — or use the quarry find tool with
  collection="{memory_collection}", agent_handle="{handle}"

To persist something you learned:
  /remember <content> — or use the quarry remember tool with
  collection="{memory_collection}", agent_handle="{handle}",
  memory_type=fact|observation|procedure|opinion

Memory types:
- fact: objective, verifiable information ("the API rate limit is 100 req/s")
- observation: neutral summary of an entity or system
- procedure: how-to knowledge ("when deploying, run migrations first")
- opinion: subjective assessment with confidence
"""


def _session_context_literal_block(handle: str, memory_collection: str) -> str:
    """Return a YAML literal block scalar fragment for session_context.

    The fragment starts with a newline so it appends cleanly to an existing
    file that may or may not end with a newline.  Each body line is indented
    two spaces as required for a YAML literal block scalar.
    """
    body = _SESSION_CONTEXT_TEMPLATE.format(
        handle=handle,
        memory_collection=memory_collection,
    )
    indented = "\n".join(f"  {line}" for line in body.splitlines())
    return f"\nsession_context: |\n{indented}\n"


def _write_ethos_ext_session_context(
    quarry_yaml: Path,
    handle: str,
) -> str:
    """Write session_context into one quarry.yaml if missing.

    Returns:
        "updated"      — session_context was appended
        "already_set"  — session_context key already present, file unchanged
        "no_collection"— memory_collection absent, nothing to do
    """
    import yaml  # noqa: PLC0415

    raw = quarry_yaml.read_text(encoding="utf-8")

    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        return "no_collection"
    if "session_context" in data:
        return "already_set"

    memory_collection = data.get("memory_collection")
    if not memory_collection:
        return "no_collection"

    fragment = _session_context_literal_block(handle, str(memory_collection))
    with quarry_yaml.open("a", encoding="utf-8") as fh:
        fh.write(fragment)
    return "updated"


def _ethos_ext_message(
    updated: list[str],
    already_set: list[str],
    no_collection: list[str],
    failed: list[str],
) -> str:
    """Build the result message for _configure_ethos_ext."""

    def _plural(lst: list[str]) -> str:
        return "identity" if len(lst) == 1 else "identities"

    parts: list[str] = []
    if updated:
        parts.append(f"updated {len(updated)} {_plural(updated)}: {', '.join(updated)}")
    if already_set:
        if not updated:
            parts.append(f"session_context already set: {', '.join(already_set)}")
        else:
            parts.append(f"already set: {', '.join(already_set)}")
    if no_collection:
        parts.append(f"no memory_collection (check config): {', '.join(no_collection)}")
    if failed:
        parts.append(f"errors: {'; '.join(failed)}")
    return "; ".join(parts)


def _scan_identities_dir(
    identities_dir: Path,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Iterate identity ext dirs and classify each quarry.yaml.

    Returns (updated, already_set, no_collection, failed).
    """
    updated: list[str] = []
    already_set: list[str] = []
    no_collection: list[str] = []
    failed: list[str] = []

    for ext_dir in sorted(identities_dir.iterdir()):
        if not ext_dir.is_dir() or not ext_dir.name.endswith(".ext"):
            continue
        handle = ext_dir.name[: -len(".ext")]
        quarry_yaml = ext_dir / "quarry.yaml"
        if not quarry_yaml.exists():
            continue
        try:
            result = _write_ethos_ext_session_context(quarry_yaml, handle)
            if result == "updated":
                updated.append(handle)
            elif result == "already_set":
                already_set.append(handle)
            elif result == "no_collection":
                no_collection.append(handle)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{handle}: {exc}")

    return updated, already_set, no_collection, failed


def _configure_ethos_ext(
    identities_dir: Path | None = None,
) -> CheckResult:
    """Write session_context into ethos ext quarry.yaml for each configured identity.

    Idempotent: leaves existing session_context keys unchanged. Skips identity
    directories that have no quarry.yaml (quarry not configured for that identity).
    """
    if identities_dir is None:
        identities_dir = Path.home() / ".punt-labs" / "ethos" / "identities"

    if not identities_dir.exists():
        return CheckResult(
            name="Ethos ext session_context",
            passed=True,
            message="ethos not installed, skipping",
            required=False,
        )

    updated, already_set, no_collection, failed = _scan_identities_dir(identities_dir)

    if not updated and not already_set and not no_collection and not failed:
        return CheckResult(
            name="Ethos ext session_context",
            passed=True,
            message="no identities with quarry configured",
            required=False,
        )

    return CheckResult(
        name="Ethos ext session_context",
        passed=not failed,
        message=_ethos_ext_message(updated, already_set, no_collection, failed),
        required=False,
    )


def run_install() -> int:  # noqa: C901
    """Create data directory, download model, and configure MCP clients.

    Returns 0 on success, 1 on failure.
    """
    print(f"punt-quarry {_quarry_version()}")  # noqa: T201
    print()  # noqa: T201

    failed = False

    # Step 1: data + logs directories
    data_dir = Path.home() / ".punt-labs" / "quarry" / "data" / "default" / "lancedb"
    logs_dir = Path.home() / ".punt-labs" / "quarry" / "logs"
    print("[1/8] Creating directories...")  # noqa: T201
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        print(f"  \u2713 {data_dir}")  # noqa: T201
        print(f"  \u2713 {logs_dir}")  # noqa: T201
    except OSError as exc:
        print(f"  \u2717 Failed to create directories: {exc}")  # noqa: T201
        failed = True

    # Step 2: GPU runtime (must run before model download so CUDA provider
    # detection can trigger FP16 model caching)
    print("[2/8] Checking GPU runtime...")  # noqa: T201
    try:
        from quarry.service import ensure_gpu_runtime  # noqa: PLC0415

        gpu_status = ensure_gpu_runtime()
        if "failed" in gpu_status:
            print(f"  \u2717 {gpu_status}")  # noqa: T201
            failed = True
        else:
            print(f"  \u2713 {gpu_status}")  # noqa: T201
    except Exception as exc:  # noqa: BLE001
        print(f"  \u2022 Skipped: {exc}")  # noqa: T201

    # Step 3: embedding model
    print("[3/8] Downloading embedding model...")  # noqa: T201
    try:
        from quarry.embeddings import download_model_files  # noqa: PLC0415

        download_model_files()
        print("  \u2713 snowflake-arctic-embed-m-v1.5 (INT8 ONNX) cached")  # noqa: T201
        # Also download FP16 model if CUDA is available.
        # NOTE: This is an in-process import. If onnxruntime was already
        # imported earlier in this process *before* ensure_gpu_runtime()
        # swapped the package in step 2, the native shared libraries (.so)
        # from the old onnxruntime remain loaded and provider detection here
        # may be stale. In a typical `quarry install` run where onnxruntime
        # has not yet been imported in-process, this is accurate. The FP16
        # model will be downloaded on the next run if needed.
        try:
            import onnxruntime as ort  # noqa: PLC0415

            if "CUDAExecutionProvider" in ort.get_available_providers():
                download_model_files(model_file="onnx/model_fp16.onnx")
                print("  \u2713 FP16 model cached (for CUDA)")  # noqa: T201
        except Exception:  # noqa: BLE001, S110
            pass  # FP16 download is optional -- first-use fallback works
    except Exception as exc:  # noqa: BLE001
        print(f"  \u2717 Model download failed: {exc}")  # noqa: T201
        failed = True

    # Step 4: mcp-proxy binary (best-effort — proxy is optional, falls back to direct)
    # Installed before MCP client config so Desktop can resolve the absolute path.
    print("[4/8] Installing mcp-proxy...")  # noqa: T201
    try:
        from quarry.proxy import install as proxy_install  # noqa: PLC0415

        msg = proxy_install()
        print(f"  \u2713 {msg}")  # noqa: T201
    except Exception as exc:  # noqa: BLE001
        print(f"  \u2022 Skipped: {exc}")  # noqa: T201
        print("    mcp-proxy is optional — quarry works without it.")  # noqa: T201

    # Step 5: MCP clients (uses mcp-proxy if step 4 succeeded, otherwise quarry mcp)
    print("[5/8] Configuring MCP clients...")  # noqa: T201
    for check in [_configure_claude_code(), _configure_claude_desktop()]:
        _print_check(check)

    # Step 6: daemon service (best-effort — not available in CI, containers, SSH)
    print("[6/8] Registering quarry daemon...")  # noqa: T201
    try:
        from quarry.service import install as svc_install  # noqa: PLC0415

        msg = svc_install()
        print(f"  \u2713 {msg}")  # noqa: T201
    except Exception as exc:  # noqa: BLE001
        print(f"  \u2022 Skipped: {exc}")  # noqa: T201
        print("    Daemon registration is optional — quarry works without it.")  # noqa: T201

    # Step 7: CLAUDE.md context injection (best-effort)
    print("[7/8] Injecting quarry context into CLAUDE.md...")  # noqa: T201
    try:
        msg = _inject_claude_md()
        print(f"  \u2713 {msg}")  # noqa: T201
    except Exception as exc:  # noqa: BLE001
        print(f"  \u2022 Skipped: {exc}")  # noqa: T201

    # Step 8: ethos ext session_context (best-effort)
    print("[8/8] Configuring ethos identity extension...")  # noqa: T201
    try:
        check = _configure_ethos_ext()
        _print_check(check)
    except Exception as exc:  # noqa: BLE001
        print(f"  \u2022 Skipped: {exc}")  # noqa: T201
        print("    Ethos extension configuration is optional.")  # noqa: T201

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
            _check_provider(),
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
