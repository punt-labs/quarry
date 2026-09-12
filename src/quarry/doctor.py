"""Environment diagnostics and install: verify deps, download model, configure."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Generator
from pathlib import Path

from quarry.doctor_captures import CaptureDiagnostics
from quarry.doctor_daemon import DaemonDiagnostics
from quarry.doctor_ethos import EthosExtDiagnostics
from quarry.doctor_inference import InferenceDiagnostics
from quarry.doctor_memory import MemoryDiagnostics
from quarry.doctor_sync import SyncDiagnostics
from quarry.results import CheckResult


def _quarry_version() -> str:
    from importlib.metadata import version  # noqa: PLC0415

    return version("punt-quarry")


@contextlib.contextmanager
def _quiet_logging() -> Generator[None]:
    """Temporarily suppress third-party logging during checks.

    RapidOCR adds its own StreamHandler that writes to stderr during init.
    Setting root logger level isn't enough — we must redirect stderr to
    suppress output from handlers created after our context enters.
    """
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.CRITICAL)
    try:
        with Path(os.devnull).open("w") as devnull, contextlib.redirect_stderr(devnull):
            yield
    finally:
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


def _check_imports() -> CheckResult:
    # OCR's modules (rapidocr, cv2) are deliberately absent: they are an optional
    # capability, and importing rapidocr can transitively load the GUI-linked cv2
    # that fails on a headless box. Their absence must not fail this required
    # check.
    modules = [
        "lancedb",
        "tokenizers",
        "huggingface_hub",
        "pymupdf",
        "PIL",
        "onnxruntime",
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


def _human_size(nbytes: float) -> str:
    """Format byte count as human-readable string (float: /=1024 stays in-type)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024 or unit == "TB":
            return f"{nbytes:.1f} {unit}" if nbytes >= 10 else f"{nbytes:.2f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"  # unreachable but satisfies type checker


def _check_fts_health(db_path: Path) -> CheckResult:
    """Verify the Tantivy FTS index is queryable."""
    from quarry.db.facade import Database  # noqa: PLC0415
    from quarry.db.schema import TABLE_NAME  # noqa: PLC0415

    if not db_path.exists():
        return CheckResult(
            name="FTS index",
            passed=True,
            message="no database yet",
            required=False,
        )
    try:
        database = Database.connect(db_path)
        if TABLE_NAME not in database.db.list_tables().tables:
            return CheckResult(
                name="FTS index",
                passed=True,
                message="no table yet",
                required=False,
            )
        table = database.db.open_table(TABLE_NAME)
        table.search("health", query_type="fts").limit(1).to_list()
        return CheckResult(
            name="FTS index",
            passed=True,
            message="healthy",
            required=False,
        )
    except RuntimeError:
        return CheckResult(
            name="FTS index",
            passed=False,
            message="stale — run 'quarry sync' to rebuild",
            required=False,
        )
    except (OSError, ValueError):
        return CheckResult(
            name="FTS index",
            passed=False,
            message="missing — will be created on next sync",
            required=False,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="FTS index",
            passed=False,
            message=f"error: {exc}",
            required=False,
        )


_MCP_SERVER_NAME = "quarry"
_SCOPE_USER = ("--scope", "user")


def _mcp_command(*, resolve_paths: bool = False) -> tuple[str, list[str]]:
    """Return the command that runs the quarry MCP server directly.

    ``quarry mcp`` is a stdio FastMCP client of the daemon (DES-031 v2.2): the
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


def _configure_claude_code() -> CheckResult:
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
    command, args = _mcp_command()
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

    command, args = _mcp_command(resolve_paths=True)
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


def _install_gpu_runtime() -> bool:
    """Run the GPU-runtime swap step of the installer; return whether it failed.

    Prints the ``GpuStatus`` line plus its :attr:`GpuStatus.install_detail`
    clause (only ``CUDA_UNSUPPORTED`` carries one — a hint pointing at the
    GPU-runtime warning log, which names the detected vs supported CUDA majors).
    Returns ``True`` only when the daemon cannot start after the check — i.e.
    ``RESTORE_FAILED`` — so the caller marks the install failed. Recovered
    outcomes (``RESTORED``, ``CUDA_UNSUPPORTED``) warn but do not fail.

    Unexpected exceptions are NOT caught here: this helper is not an error
    boundary (PY-EH-6). A raise means failure and is caught at ``run_install``,
    the installer entry point, which converts it to a hard install failure.
    """
    from quarry.gpu_runtime import GpuRuntime  # noqa: PLC0415

    gpu_status = GpuRuntime.ensure()
    print(f"  {gpu_status.symbol} {gpu_status}{gpu_status.install_detail}")  # noqa: T201
    return gpu_status.is_failure


def _run_optional_step(step: Callable[[], str], skip_note: str) -> None:
    """Run a best-effort install step, printing its result or a skip note.

    Steps like mcp-proxy and daemon registration are optional: quarry works
    without them. ``step`` is a zero-arg callable that performs BOTH the module
    import AND the install, returning a status message. It runs entirely inside
    the try so an import-time failure (``ImportError``, or any error raised while
    the optional module is loaded) skips the step rather than aborting the whole
    install — the module living behind the callable is the reason the import is
    here and not at call time. The broad catch is intentional: this IS the
    installer's optional-step boundary (PY-EH-6).
    """
    try:
        print(f"  ✓ {step()}")  # noqa: T201
    except Exception as exc:  # noqa: BLE001
        print(f"  • Skipped: {exc}")  # noqa: T201
        print(f"    {skip_note}")  # noqa: T201


def _install_embedding_model() -> bool:
    """Download the INT8 model (and FP16 on CUDA); return whether it succeeded.

    The FP16 download is best-effort — its failure is swallowed because
    first-use falls back to INT8. Only an INT8 failure marks the step failed.

    NOTE: the CUDA probe is an in-process import. If onnxruntime was imported
    earlier in this process (before ``GpuRuntime.ensure()`` swapped the package),
    the old native libraries stay loaded and provider detection here may be
    stale; a fresh ``quarry install`` has not imported it yet, so this is
    accurate, and the FP16 model is fetched on the next run if needed.
    """
    try:
        from quarry.embeddings import OnnxEmbeddingBackend  # noqa: PLC0415

        OnnxEmbeddingBackend.download_model_files()
        print("  ✓ snowflake-arctic-embed-m-v1.5 (INT8 ONNX) cached")  # noqa: T201
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ Model download failed: {exc}")  # noqa: T201
        return False
    try:
        import onnxruntime as ort  # noqa: PLC0415

        if "CUDAExecutionProvider" in ort.get_available_providers():
            OnnxEmbeddingBackend.download_model_files(model_file="onnx/model_fp16.onnx")
            print("  ✓ FP16 model cached (for CUDA)")  # noqa: T201
    except Exception:  # noqa: BLE001, S110
        pass  # FP16 download is optional -- first-use fallback works
    return True


def run_install() -> int:
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

    # Step 2: headless OpenCV (rapidocr hard-requires the GUI opencv-python;
    # force the headless wheel to own cv2 so OCR loads on a screenless box, the
    # package-side equivalent of install.sh's resolver override). Best-effort:
    # a failure prints the exact reinstall command and OCR degrades cleanly via
    # the runtime guard.
    print("[2/8] Ensuring headless OpenCV...")  # noqa: T201
    from quarry.opencv_headless import HeadlessOpenCv  # noqa: PLC0415

    headless = HeadlessOpenCv(sys.executable)
    _run_optional_step(
        headless.enforce,
        f"OCR degrades cleanly; to enable it run: {headless.remediation()}",
    )

    # Step 3: GPU runtime (must run before model download so CUDA provider
    # detection can trigger FP16 model caching). The broad catch is the
    # installer's error boundary (PY-EH-6): every legitimate GPU outcome
    # returns a GpuStatus, so a raise here is unexpected and may leave a broken
    # runtime — convert it to a hard install failure rather than skip.
    print("[3/8] Checking GPU runtime...")  # noqa: T201
    try:
        if _install_gpu_runtime():
            failed = True
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ GPU runtime check failed: {exc}")  # noqa: T201
        failed = True

    # Step 4: embedding model
    print("[4/8] Downloading embedding model...")  # noqa: T201
    if not _install_embedding_model():
        failed = True

    # Step 5: mcp-proxy binary (best-effort — proxy is optional, falls back to direct)
    # Installed before MCP client config so Desktop can resolve the absolute path.
    print("[5/8] Installing mcp-proxy...")  # noqa: T201

    def _proxy_step() -> str:
        # Import INSIDE the callable so an import-time failure of quarry.proxy
        # skips this optional step instead of aborting run_install.
        from quarry.proxy import install as proxy_install  # noqa: PLC0415

        return proxy_install()

    _run_optional_step(_proxy_step, "mcp-proxy is optional — quarry works without it.")

    # Step 6: MCP clients (uses mcp-proxy if step 5 succeeded, otherwise quarry mcp)
    print("[6/8] Configuring MCP clients...")  # noqa: T201
    for check in [_configure_claude_code(), _configure_claude_desktop()]:
        _print_check(check)

    # Step 7: daemon service (best-effort — not available in CI, containers, SSH)
    print("[7/8] Registering quarry daemon...")  # noqa: T201

    def _svc_step() -> str:
        # Import INSIDE the callable so an import-time failure of quarry.service
        # skips this optional step instead of aborting run_install.
        from quarry.service import install as svc_install  # noqa: PLC0415

        return svc_install()

    _run_optional_step(
        _svc_step, "Daemon registration is optional — quarry works without it."
    )

    # quarry is repo-scoped for CLAUDE.md guidance: `quarry enable` registers the
    # per-repo @-import; `install` never edits ~/.claude/CLAUDE.md.

    # Step 8: ethos ext session_context (best-effort)
    print("[8/8] Configuring ethos identity extension...")  # noqa: T201
    try:
        check = EthosExtDiagnostics.configure()
        _print_check(check)
    except Exception as exc:  # noqa: BLE001
        print(f"  \u2022 Skipped: {exc}")  # noqa: T201
        print("    Ethos extension configuration is optional.")  # noqa: T201

    # Verification
    print("\nVerifying installation...")  # noqa: T201
    exit_code = check_environment(_skip_header=True)
    return 1 if failed else exit_code


def check_environment(*, _skip_header: bool = False) -> int:
    """Run all environment checks. Returns 0 if all required pass, 1 otherwise."""
    if not _skip_header:
        print(f"punt-quarry {_quarry_version()}")  # noqa: T201
        print()  # noqa: T201

    from quarry.config import Settings  # noqa: PLC0415

    settings = Settings()
    cwd = str(Path.cwd())
    with _quiet_logging():
        all_results: list[CheckResult | None] = [
            _check_python_version(),
            _check_data_directory(),
            _check_embedding_model(),
            InferenceDiagnostics.onnx_provider(),
            _check_imports(),
            _check_mcp_proxy(),
            _check_claude_code_mcp(),
            _check_claude_desktop_mcp(),
            DaemonDiagnostics.reachability(),
            DaemonDiagnostics.serve_token(),
            DaemonDiagnostics.fd_headroom(),
            _check_fts_health(settings.lancedb_path),
            SyncDiagnostics.recency(settings.registry_path),
            SyncDiagnostics.directories(settings.registry_path),
            SyncDiagnostics.enable_status(settings.registry_path, cwd),
            CaptureDiagnostics.unlinked(settings.registry_path, settings.lancedb_path),
            CaptureDiagnostics.shadow_repo(cwd),
            MemoryDiagnostics.corpus(settings.lancedb_path),
            MemoryDiagnostics.identity_active(cwd, settings.lancedb_path),
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
