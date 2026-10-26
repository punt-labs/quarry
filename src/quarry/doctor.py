"""Environment diagnostics and install: verify deps, download model, configure."""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from collections.abc import Callable, Generator
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from quarry.config import (
    ONNX_MODEL_REPO,
    ONNX_MODEL_REVISION,
    ONNX_TOKENIZER_FILE,
    Settings,
)
from quarry.doctor_captures import CaptureDiagnostics
from quarry.doctor_daemon import DaemonDiagnostics
from quarry.doctor_ethos import EthosExtDiagnostics
from quarry.doctor_failure import CheckFailure
from quarry.doctor_inference import InferenceDiagnostics
from quarry.doctor_mcp import ClaudeCodeMcp, ClaudeDesktopMcp
from quarry.doctor_memory import MemoryDiagnostics
from quarry.doctor_recall import RecallDiagnostics
from quarry.doctor_sync import SyncDiagnostics
from quarry.gpu_runtime import GpuRuntime
from quarry.opencv_headless import HeadlessOpenCv
from quarry.redacted_failure import RedactedFailure
from quarry.results import CheckResult

logger = logging.getLogger(__name__)


def _quarry_version() -> str:
    """Return the installed version, or ``unknown`` when there is no metadata."""
    try:
        return version("punt-quarry")
    except PackageNotFoundError:
        return "unknown"


# The third-party loggers whose output the checks drown the report in, by the
# names the libraries create them under.  Only these are silenced: quarry's own
# errors (a check that failed unexpectedly) must still reach the log file.
_QUIETED_LOGGERS = (
    "RapidOCR",
    "huggingface_hub",
    "urllib3",
    "httpx",
    "PIL",
)


@contextlib.contextmanager
def _quiet_logging() -> Generator[None]:
    """Temporarily suppress third-party logging during checks.

    RapidOCR adds its own StreamHandler that writes to stderr during init, so
    the named third-party loggers are raised to CRITICAL and stderr is redirected
    to suppress output from handlers created after our context enters.  The root
    logger is left alone, so quarry's own ERROR records still reach the file
    handler.
    """
    loggers = [logging.getLogger(name) for name in _QUIETED_LOGGERS]
    previous = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.CRITICAL)
    try:
        with Path(os.devnull).open("w") as devnull, contextlib.redirect_stderr(devnull):
            yield
    finally:
        for logger, level in zip(loggers, previous, strict=True):
            logger.setLevel(level)


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
    # Imported here, inside a try: a broken install is a failed check, not an
    # import error that takes the whole report down.
    try:
        from huggingface_hub import try_to_load_from_cache  # noqa: PLC0415
    except CheckFailure.BROKEN_STACK as exc:
        return CheckResult(
            name="Embedding model",
            passed=False,
            message=CheckFailure.import_message("huggingface_hub", exc),
        )
    try:
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
    except CheckFailure.BROKEN_STACK as exc:
        return CheckResult(
            name="Embedding model",
            passed=False,
            message=f"cannot read the model cache ({CheckFailure.detail(exc)})",
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
        except CheckFailure.BROKEN_STACK:
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
    # LanceDB and pyarrow are native: a broken install is a failed check, not an
    # import error that takes the whole report down.
    try:
        from quarry.db.facade import Database  # noqa: PLC0415
        from quarry.db.schema import TABLE_NAME  # noqa: PLC0415
    except CheckFailure.BROKEN_STACK as exc:
        return CheckResult(
            name="FTS index",
            passed=False,
            message=CheckFailure.import_message("quarry.db", exc),
            required=False,
        )

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
    except RuntimeError as exc:
        return CheckResult(
            name="FTS index",
            passed=False,
            message=(
                f"stale — run 'quarry sync' to rebuild ({CheckFailure.detail(exc)})"
            ),
            required=False,
        )
    except (FileNotFoundError, ValueError) as exc:
        return CheckResult(
            name="FTS index",
            passed=False,
            message=(
                f"missing — will be created on next sync ({CheckFailure.detail(exc)})"
            ),
            required=False,
        )
    except OSError as exc:
        return CheckResult(
            name="FTS index",
            passed=False,
            message=f"cannot read the index ({CheckFailure.detail(exc)})",
            required=False,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="FTS index",
            passed=False,
            message=f"error: {CheckFailure.detail(exc)}",
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
    boundary. A raise means failure and is caught at ``run_install``,
    the installer entry point, which converts it to a hard install failure.
    """
    gpu_status = GpuRuntime.ensure()
    print(f"  {gpu_status.symbol} {gpu_status}{gpu_status.install_detail}")  # noqa: T201
    return gpu_status.is_failure


def _run_optional_step(step: Callable[[], str], skip_note: str) -> None:
    """Run a best-effort install step, printing its result or a skip note.

    Optional steps are ones quarry works without.
    ``step`` is a zero-arg callable that performs BOTH the module import AND
    the install, returning a status message. It runs entirely inside the try so
    an import-time failure (``ImportError``, or any error raised while the
    optional module is loaded) skips the step rather than aborting the whole
    install — the module living behind the callable is the reason the import is
    here and not at call time. The broad catch is intentional: this IS the
    installer's optional-step boundary.
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
        # Imports onnxruntime, which may be absent or broken: the failure is the
        # step's result (below), not an import error.
        from quarry.embeddings import OnnxEmbeddingBackend  # noqa: PLC0415

        OnnxEmbeddingBackend.download_model_files()
        print("  ✓ snowflake-arctic-embed-m-v1.5 (INT8 ONNX) cached")  # noqa: T201
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ Model download failed: {exc}")  # noqa: T201
        return False
    try:
        # Optional dependency (onnxruntime): absent means no CUDA, not a failure.
        import onnxruntime as ort  # noqa: PLC0415

        if "CUDAExecutionProvider" in ort.get_available_providers():
            OnnxEmbeddingBackend.download_model_files(model_file="onnx/model_fp16.onnx")
            print("  ✓ FP16 model cached (for CUDA)")  # noqa: T201
    except Exception:  # noqa: BLE001, S110
        pass  # FP16 download is optional -- first-use fallback works
    return True


def _create_directories() -> bool:
    """Create the data and logs directories; return whether both now exist."""
    data_dir = Path.home() / ".punt-labs" / "quarry" / "data" / "default" / "lancedb"
    logs_dir = Path.home() / ".punt-labs" / "quarry" / "logs"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"  \u2717 Failed to create directories: {exc}")  # noqa: T201
        return False
    print(f"  \u2713 {data_dir}")  # noqa: T201
    print(f"  \u2713 {logs_dir}")  # noqa: T201
    return True


def _gpu_runtime_is_usable() -> bool:
    """Run the GPU runtime step; return False when the install must fail.

    The broad catch is the installer's error boundary: every legitimate GPU
    outcome returns a GpuStatus, so a raise here is unexpected and may leave a
    broken runtime — it becomes a hard install failure rather than a skip.
    """
    try:
        return not _install_gpu_runtime()
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ GPU runtime check failed: {exc}")  # noqa: T201
        return False


def _configure_ethos_extension() -> None:
    """Configure the ethos identity extension; it is optional, so never fail."""
    try:
        _print_check(EthosExtDiagnostics.configure())
    except Exception as exc:  # noqa: BLE001
        print(f"  \u2022 Skipped: {exc}")  # noqa: T201
        print("    Ethos extension configuration is optional.")  # noqa: T201


def run_install() -> int:
    """Create data directory, download model, and configure MCP clients.

    Returns 0 on success, 1 on failure.
    """
    print(f"punt-quarry {_quarry_version()}")  # noqa: T201
    print()  # noqa: T201

    # Step 1: data + logs directories
    print("[1/7] Creating directories...")  # noqa: T201
    failed = not _create_directories()

    # Step 2: headless OpenCV (rapidocr hard-requires the GUI opencv-python;
    # force the headless wheel to own cv2 so OCR loads on a screenless box, the
    # package-side equivalent of install.sh's resolver override). Best-effort:
    # a failure prints the exact reinstall command and OCR degrades cleanly via
    # the runtime guard.
    print("[2/7] Ensuring headless OpenCV...")  # noqa: T201
    headless = HeadlessOpenCv(sys.executable)
    _run_optional_step(
        headless.enforce,
        f"OCR degrades cleanly; to enable it run: {headless.remediation()}",
    )

    # Step 3: GPU runtime (must run before model download so CUDA provider
    # detection can trigger FP16 model caching).
    print("[3/7] Checking GPU runtime...")  # noqa: T201
    if not _gpu_runtime_is_usable():
        failed = True

    # Step 4: embedding model
    print("[4/7] Downloading embedding model...")  # noqa: T201
    if not _install_embedding_model():
        failed = True

    # Step 5: MCP clients
    print("[5/7] Configuring MCP clients...")  # noqa: T201
    for check in [ClaudeCodeMcp().configure(), ClaudeDesktopMcp().configure()]:
        _print_check(check)

    # Step 6: daemon service (best-effort — not available in CI, containers, SSH)
    print("[6/7] Registering quarry daemon...")  # noqa: T201

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

    # Step 7: ethos ext session_context (best-effort)
    print("[7/7] Configuring ethos identity extension...")  # noqa: T201
    _configure_ethos_extension()

    # Verification
    print("\nVerifying installation...")  # noqa: T201
    exit_code = check_environment(_skip_header=True)
    return 1 if failed else exit_code


def _attempt(name: str, check: Callable[[], CheckResult | None]) -> CheckResult | None:
    """Run one check; whatever it raises becomes a failed result naming it.

    The report must always complete, so no single check, whatever it hits, may
    stop the others from running.  The failure is also logged with its frames.
    """
    try:
        return check()
    except Exception as exc:
        logger.warning(
            "doctor: check %s failed", name, exc_info=RedactedFailure.of(exc)
        )
        return CheckResult(
            name=name,
            passed=False,
            message=f"check could not run ({CheckFailure.detail(exc)})",
            required=False,
        )


def _settings_checks(cwd: str) -> list[tuple[str, Callable[[], CheckResult | None]]]:
    """Return the checks that need the settings, or one failed check without them."""
    try:
        settings = Settings()
    except Exception as exc:
        logger.warning("doctor: settings are invalid", exc_info=RedactedFailure.of(exc))
        failed = CheckResult(
            name="Settings",
            passed=False,
            message=f"invalid settings ({CheckFailure.detail(exc)})",
        )
        return [("Settings", lambda: failed)]
    return [
        ("FTS index", lambda: _check_fts_health(settings.lancedb_path)),
        (
            "Sync directories",
            lambda: SyncDiagnostics.directories(settings.registry_path),
        ),
        (
            "Sync enable",
            lambda: SyncDiagnostics.enable_status(settings.registry_path, cwd),
        ),
        (
            "Unlinked captures",
            lambda: CaptureDiagnostics.unlinked(
                settings.registry_path, settings.lancedb_path
            ),
        ),
        ("Shadow repo", lambda: CaptureDiagnostics.shadow_repo(cwd)),
        ("Memory corpus", lambda: MemoryDiagnostics.corpus(settings.lancedb_path)),
        (
            "Memory identity",
            lambda: MemoryDiagnostics.identity_active(cwd, settings.lancedb_path),
        ),
        (
            "Recall activity",
            lambda: RecallDiagnostics.recent_activity(
                settings.telemetry_path,
                telemetry_enabled=settings.telemetry_enabled,
            ),
        ),
    ]


def _run_checks() -> list[CheckResult]:
    """Run every environment check with third-party logging quieted."""
    cwd = str(Path.cwd())
    independent: list[tuple[str, Callable[[], CheckResult | None]]] = [
        ("Python", _check_python_version),
        ("Data directory", _check_data_directory),
        ("Embedding model", _check_embedding_model),
        ("ONNX provider", InferenceDiagnostics.onnx_provider),
        ("Core imports", _check_imports),
        ("Claude Code MCP", ClaudeCodeMcp().check),
        ("Claude Desktop MCP", ClaudeDesktopMcp().check),
        ("Daemon", DaemonDiagnostics.reachability),
        ("Serve token", DaemonDiagnostics.serve_token),
        ("Fd headroom", DaemonDiagnostics.fd_headroom),
        ("Stop timeout", DaemonDiagnostics.stop_timeout),
    ]
    with _quiet_logging():
        results = [
            _attempt(name, check)
            for name, check in [*independent, *_settings_checks(cwd)]
        ]
    return [result for result in results if result is not None]


def check_environment(*, _skip_header: bool = False) -> int:
    """Run all environment checks. Returns 0 if all required pass, 1 otherwise."""
    if not _skip_header:
        print(f"punt-quarry {_quarry_version()}")  # noqa: T201
        print()  # noqa: T201

    checks = _run_checks()
    for check in checks:
        _print_check(check)

    required_failures = [c for c in checks if c.required and not c.passed]
    if required_failures:
        print(f"\n{len(required_failures)} issue(s) found.")  # noqa: T201
        return 1
    print("\nAll checks passed.")  # noqa: T201
    return 0
