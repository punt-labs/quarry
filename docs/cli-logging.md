# CLI Logging UX -- Implementation Design

Task-level implementation plan for the CLI logging UX overhaul defined
in `docs/cli-logging-ux.md`. Each item specifies exact code changes,
line numbers (as of commit `324841d`), and test cases.

---

## Dependency Graph

```text
Phase 1a: configure_logging move (6.4)
    |
    v
Phase 1b: _progress to stderr (6.1) + --quiet suppression (6.3)
    |         + --verbose wiring (6.2)
    |         (atomic -- land together)
    |
    +--> Phase 2a: uninstall_cmd stdout -> _emit (6.7)
    |    Phase 2b: login_cmd abort -> err_console (6.8)
    |    Phase 2c: delete console = Console() (6.7 cleanup)
    |      (2a-2c are independent of each other but 2c depends on 2a)
    |
    +--> Phase 3: remote-path --quiet guards (6.9)
    |      (depends on Phase 1b for _quiet semantics)
    |
    +--> Phase 4a: remember_cmd progress wrapper (6.10)
    |    Phase 4b: --verbose help text (6.11)
    |    Phase 4c: QUARRY_LOG_LEVEL env var (8.7 escape hatch)
    |      (independent of each other, depend on Phase 1a)
```

Implementation order: 1a, then 1b, then 2a+2b+2c, then 3, then 4a+4b+4c.

---

## Item 1a: Move `configure_logging` from module-level to `main_callback`

**Architecture ref**: Section 6.4

### Current code (line 78)

```python
configure_logging(stderr_level="WARNING")
logger = logging.getLogger(__name__)
```

### Target code (line 78)

```python
logger = logging.getLogger(__name__)
```

The `configure_logging(stderr_level="WARNING")` call at line 78 is deleted.
The `logger = logging.getLogger(__name__)` at line 79 stays at module level
(it does not trigger handler creation).

### Target code in `main_callback` (after line 186, before line 187)

Insert after the `_global_db = database` assignment and before the
`if ctx.invoked_subcommand is None` check:

```python
    # Determine stderr log level from flags.
    if _verbose:
        stderr_level = "INFO"
    elif _quiet:
        stderr_level = "CRITICAL"
    else:
        stderr_level = "WARNING"
    configure_logging(stderr_level=stderr_level)
```

The full `main_callback` body becomes:

```python
def main_callback(...) -> None:
    """quarry: extract searchable knowledge from any document."""
    global _json_output, _verbose, _quiet, _global_db
    if verbose and quiet:
        err_console.print("Error: --verbose and --quiet are mutually exclusive.")
        raise typer.Exit(code=1)
    _json_output = output_json
    _verbose = verbose
    _quiet = quiet
    _global_db = database
    # Determine stderr log level from flags.
    if _verbose:
        stderr_level = "INFO"
    elif _quiet:
        stderr_level = "CRITICAL"
    else:
        stderr_level = "WARNING"
    configure_logging(stderr_level=stderr_level)
    if ctx.invoked_subcommand is None:
        print(ctx.get_help())
        raise typer.Exit(code=0)
```

Remove the comments "reserved: commands will use for extra output" and
"reserved: commands will use to suppress non-essential output" from lines
184-185 -- these flags are no longer reserved; they are wired.

### Tests

**File**: `tests/test_cli.py`

**Test 1**: `test_configure_logging_called_in_main_callback`

```python
def test_configure_logging_called_in_main_callback():
    """configure_logging is called from main_callback, not at import time."""
    _reset_globals()
    with (
        patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
        patch("quarry.__main__.get_db"),
        patch("quarry.__main__.list_documents", return_value=[]),
        patch("quarry.__main__.configure_logging") as mock_cfg,
    ):
        result = runner.invoke(app, ["list", "documents"])
    assert result.exit_code == 0
    mock_cfg.assert_called_once_with(stderr_level="WARNING")
```

Asserts: `configure_logging` is called exactly once per invocation with
the default `stderr_level="WARNING"`.

**Test 2**: `test_configure_logging_not_called_at_import`

```python
def test_configure_logging_not_called_at_import():
    """Module-level code does not call configure_logging."""
    import ast
    src = Path("src/quarry/__main__.py").read_text()
    tree = ast.parse(src)
    # Walk top-level statements only (not inside functions/classes)
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            assert name != "configure_logging", (
                f"configure_logging called at module level (line {node.lineno})"
            )
```

Asserts: no top-level (module-scope) call to `configure_logging` exists
in the AST. This is a structural test that prevents regression.

---

## Item 1b: `_progress` to stderr + `--quiet` suppression + `--verbose` wiring

**Architecture ref**: Sections 6.1, 6.2, 6.3

These three changes are atomic. Applying 6.1 without 6.3 violates the
quiet contract. Applying 6.2 without 6.4 (Item 1a) means there is nowhere
to call `configure_logging`.

### 6.1 -- `_progress` to stderr (lines 206-224)

**Current code**:

```python
@contextlib.contextmanager
def _progress(
    label: str,
) -> Generator[Callable[[str], None] | None]:
    """Yield a progress callback, or None in JSON mode.

    In human mode the Rich progress bar is started and guaranteed to stop
    on exit (including exceptions).  In JSON mode nothing is rendered.
    """
    if _json_output:
        yield None
        return
    p = Progress(console=console)
    task = p.add_task(label, total=None)
    p.start()
    try:
        yield lambda message: p.update(task, description=message)
    finally:
        p.stop()
```

**Target code**:

```python
@contextlib.contextmanager
def _progress(
    label: str,
) -> Generator[Callable[[str], None] | None]:
    """Yield a progress callback, or None when output is suppressed.

    The Rich progress bar renders on stderr.  It is suppressed in
    ``--json`` mode (no visual noise alongside machine output) and in
    ``--quiet`` mode (stderr contract: only fatal errors).
    """
    if _json_output or _quiet:
        yield None
        return
    p = Progress(console=err_console)
    task = p.add_task(label, total=None)
    p.start()
    try:
        yield lambda message: p.update(task, description=message)
    finally:
        p.stop()
```

Changes:

1. `if _json_output:` becomes `if _json_output or _quiet:` (6.3 quiet suppression)
2. `Progress(console=console)` becomes `Progress(console=err_console)` (6.1 stderr)
3. Docstring updated to reflect both suppression conditions

### 6.2 -- `--verbose` wiring

Already handled by Item 1a. When `--verbose` is set, `main_callback` calls
`configure_logging(stderr_level="INFO")`. No additional code is needed in
`_progress` or any command function. All existing `logger.info()` calls in
sync.py, pipeline.py, database.py, and embeddings.py become visible on
stderr automatically.

### Tests

**File**: `tests/test_cli.py`

**Test 3**: `test_progress_uses_stderr_console`

```python
def test_progress_uses_stderr_console():
    """_progress creates a Rich Progress bar on err_console (stderr)."""
    _reset_globals()
    cli_mod._json_output = False
    cli_mod._quiet = False
    with patch("quarry.__main__.Progress") as MockProgress:
        mock_instance = MagicMock()
        MockProgress.return_value = mock_instance
        mock_instance.add_task.return_value = 0
        with cli_mod._progress("test"):
            pass
    MockProgress.assert_called_once_with(console=cli_mod.err_console)
```

Asserts: `Progress` is constructed with `console=err_console`, not `console`.

**Test 4**: `test_progress_suppressed_in_quiet_mode`

```python
def test_progress_suppressed_in_quiet_mode():
    """_progress yields None when --quiet is set."""
    _reset_globals()
    cli_mod._quiet = True
    with cli_mod._progress("test") as cb:
        pass
    assert cb is None
```

Asserts: callback is `None` when `_quiet` is `True`.

**Test 5**: `test_progress_suppressed_in_json_mode`

```python
def test_progress_suppressed_in_json_mode():
    """_progress yields None when --json is set (existing behavior)."""
    _reset_globals()
    cli_mod._json_output = True
    with cli_mod._progress("test") as cb:
        pass
    assert cb is None
```

Asserts: existing JSON suppression still works.

**Test 6**: `test_progress_yields_callback_in_default_mode`

```python
def test_progress_yields_callback_in_default_mode():
    """_progress yields a callable in default mode."""
    _reset_globals()
    cli_mod._json_output = False
    cli_mod._quiet = False
    with patch("quarry.__main__.Progress") as MockProgress:
        mock_instance = MagicMock()
        MockProgress.return_value = mock_instance
        mock_instance.add_task.return_value = 0
        with cli_mod._progress("test") as cb:
            assert callable(cb)
```

Asserts: a callable callback is yielded in the default (non-quiet,
non-json) mode.

**Test 7**: `test_verbose_flag_sets_info_level`

```python
def test_verbose_flag_sets_info_level():
    """--verbose causes configure_logging to be called with INFO."""
    _reset_globals()
    with (
        patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
        patch("quarry.__main__.get_db"),
        patch("quarry.__main__.list_documents", return_value=[]),
        patch("quarry.__main__.configure_logging") as mock_cfg,
    ):
        result = runner.invoke(app, ["--verbose", "list", "documents"])
    assert result.exit_code == 0
    mock_cfg.assert_called_once_with(stderr_level="INFO")
```

Asserts: `configure_logging` receives `stderr_level="INFO"` when
`--verbose` is passed.

**Test 8**: `test_quiet_flag_sets_critical_level`

```python
def test_quiet_flag_sets_critical_level():
    """--quiet causes configure_logging to be called with CRITICAL."""
    _reset_globals()
    with (
        patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
        patch("quarry.__main__.get_db"),
        patch("quarry.__main__.list_documents", return_value=[]),
        patch("quarry.__main__.configure_logging") as mock_cfg,
    ):
        result = runner.invoke(app, ["--quiet", "list", "documents"])
    assert result.exit_code == 0
    mock_cfg.assert_called_once_with(stderr_level="CRITICAL")
```

Asserts: `configure_logging` receives `stderr_level="CRITICAL"` when
`--quiet` is passed.

**Test 9**: `test_verbose_quiet_mutually_exclusive` (already exists)

The existing test at `TestGlobalFlags.test_verbose_quiet_mutually_exclusive`
covers this. No new test needed.

---

## Item 2a: `uninstall_cmd` `console.print` to `_emit`

**Architecture ref**: Section 6.7

### Current code (line 1779)

```python
    msg = svc_uninstall()
    console.print(msg)
```

### Target code

```python
    msg = svc_uninstall()
    _emit({"message": msg}, msg)
```

### Tests

**File**: `tests/test_cli.py`

**Test 10**: `test_uninstall_result_on_stdout`

```python
def test_uninstall_result_on_stdout():
    """uninstall_cmd emits its result via _emit (stdout), not console (stdout Rich)."""
    _reset_globals()
    with patch(
        "quarry.__main__.svc_uninstall",
        return_value="Service removed.",
    ):
        result = runner.invoke(app, ["uninstall"])
    assert result.exit_code == 0
    assert "Service removed." in result.stdout
```

Asserts: uninstall output appears on stdout via `_emit`.

**Test 11**: `test_uninstall_json_mode`

```python
def test_uninstall_json_mode():
    """uninstall_cmd emits JSON when --json is set."""
    _reset_globals()
    with patch(
        "quarry.__main__.svc_uninstall",
        return_value="Service removed.",
    ):
        result = runner.invoke(app, ["--json", "uninstall"])
    _reset_globals()
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["message"] == "Service removed."
```

Asserts: JSON output includes the message.

---

## Item 2b: `login_cmd` abort `print()` to `err_console.print`

**Architecture ref**: Section 6.8

### Current code (line 1335)

```python
            print("Aborted. Not logged in.")
            raise typer.Exit(code=0)
```

### Target code

```python
            err_console.print("Aborted. Not logged in.")
            raise typer.Exit(code=0)
```

### Tests

**File**: `tests/test_cli.py`

**Test 12**: `test_login_abort_message_on_stderr`

```python
def test_login_abort_message_on_stderr():
    """login_cmd abort message goes to stderr, not stdout."""
    _reset_globals()
    with patch(
        "quarry.__main__.fetch_ca_cert",
        return_value=b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n",
    ), patch(
        "quarry.__main__.cert_fingerprint",
        return_value="AA:BB:CC",
    ):
        result = runner.invoke(app, ["login", "example.com"], input="n\n")
    assert result.exit_code == 0
    assert "Aborted" in result.stderr
    assert "Aborted" not in result.stdout
```

Asserts: the abort message appears on stderr, not stdout.

---

## Item 2c: Delete `console = Console()` declaration

**Architecture ref**: Section 6.7 (cleanup)

### Current code (line 127)

```python
console = Console()
err_console = Console(stderr=True)
```

### Target code (line 127)

```python
err_console = Console(stderr=True)
```

After Items 1b (progress to `err_console`) and 2a (uninstall to `_emit`),
the `console = Console()` has zero remaining consumers. Delete it.

Also remove the `Console` import if `Console` is no longer used at
module scope. Check: `err_console = Console(stderr=True)` still uses it.
So the import stays -- only the `console` variable is deleted.

### Tests

**File**: `tests/test_cli.py`

**Test 13**: `test_no_stdout_console_variable`

```python
def test_no_stdout_console_variable():
    """The module has no stdout Console instance (only err_console)."""
    assert not hasattr(cli_mod, "console"), (
        "cli_mod.console still exists -- all output should use "
        "err_console or _emit"
    )
```

Asserts: the `console` module-level variable does not exist. This is a
structural regression test.

---

## Item 3: Remote-path `--quiet` guards

**Architecture ref**: Section 6.9

Three locations need `if not _quiet:` guards.

### Location 1: Sync 409 warning (lines 1199-1202)

**Current code**:

```python
                err_console.print(
                    f"Sync already in progress: task_id={conflict_task_id}",
                    style="yellow",
                )
```

**Target code**:

```python
                if not _quiet:
                    err_console.print(
                        f"Sync already in progress: task_id={conflict_task_id}",
                        style="yellow",
                    )
```

### Location 2: Login fingerprint display (line 1329)

**Current code**:

```python
    err_console.print(f"Server CA fingerprint: {fp}")
```

**Target code**:

```python
    if not _quiet:
        err_console.print(f"Server CA fingerprint: {fp}")
```

### Location 3: Login abort message (line 1335, after Item 2b fix)

After Item 2b changes `print(...)` to `err_console.print(...)`, add a
quiet guard. The abort message is non-fatal user feedback (exit code 0),
not an error preceding exit code 1, so it falls under the quiet contract.

**Target code**:

```python
        if not confirmed:
            if not _quiet:
                err_console.print("Aborted. Not logged in.")
            raise typer.Exit(code=0)
```

### Location 4: Sync `--workers` warning (lines 1170-1174)

The architecture doc (Section 6.9) lists three locations. The `--workers`
warning in the remote sync path is also a non-fatal stderr message without
a quiet guard. It uses `style="yellow"` (a warning) and does not precede
a non-zero exit. Guard it for consistency:

**Current code**:

```python
        if workers is not None:
            err_console.print(
                "Warning: --workers is ignored when a remote quarry server is "
                "configured",
                style="yellow",
            )
```

**Target code**:

```python
        if workers is not None and not _quiet:
            err_console.print(
                "Warning: --workers is ignored when a remote quarry server is "
                "configured",
                style="yellow",
            )
```

### Tests

**File**: `tests/test_cli.py`

**Test 14**: `test_sync_409_quiet_suppresses_warning`

```python
def test_sync_409_quiet_suppresses_warning():
    """--quiet suppresses the sync 409 warning on stderr."""
    _reset_globals()
    inner_config = {
        "url": "wss://quarry.example.com:8420/mcp",
        "ca_cert": "/path/to/ca.crt",
        "headers": {"Authorization": "Bearer tok"},
    }
    proxy_config = {"quarry": inner_config}
    exc = cli_mod.RemoteError(
        409,
        'Remote quarry server returned HTTP 409: {"task_id":"abc","detail":"busy"}',
    )
    with (
        patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
        patch("quarry.__main__._remote_https_request", side_effect=exc),
    ):
        result = runner.invoke(app, ["--quiet", "sync"])
    _reset_globals()
    assert result.exit_code == 0
    assert "Sync already in progress" not in result.stderr
```

Asserts: exit code 0 (409 is non-fatal) and no warning on stderr with
`--quiet`.

**Test 15**: `test_sync_409_default_shows_warning`

```python
def test_sync_409_default_shows_warning():
    """Default mode shows the sync 409 warning on stderr."""
    _reset_globals()
    inner_config = {
        "url": "wss://quarry.example.com:8420/mcp",
        "ca_cert": "/path/to/ca.crt",
        "headers": {"Authorization": "Bearer tok"},
    }
    proxy_config = {"quarry": inner_config}
    exc = cli_mod.RemoteError(
        409,
        'Remote quarry server returned HTTP 409: {"task_id":"abc","detail":"busy"}',
    )
    with (
        patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
        patch("quarry.__main__._remote_https_request", side_effect=exc),
    ):
        result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0
    assert "Sync already in progress" in result.stderr
```

Asserts: without `--quiet`, the warning appears on stderr.

**Test 16**: `test_login_fingerprint_quiet_suppressed`

```python
def test_login_fingerprint_quiet_suppressed():
    """--quiet suppresses the fingerprint display during login."""
    _reset_globals()
    with (
        patch(
            "quarry.__main__.fetch_ca_cert",
            return_value=b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n",
        ),
        patch("quarry.__main__.cert_fingerprint", return_value="AA:BB:CC"),
        patch("quarry.__main__.validate_connection", return_value=(True, "")),
        patch("quarry.__main__.write_proxy_config"),
        patch("quarry.__main__.store_ca_cert"),
    ):
        result = runner.invoke(app, ["--quiet", "login", "example.com", "--yes"])
    _reset_globals()
    assert result.exit_code == 0
    assert "fingerprint" not in result.stderr.lower()
```

Asserts: fingerprint not shown on stderr with `--quiet`.

**Test 17**: `test_sync_workers_warning_quiet_suppressed`

```python
def test_sync_workers_warning_quiet_suppressed():
    """--quiet suppresses the --workers ignored warning in remote sync."""
    _reset_globals()
    inner_config = {
        "url": "wss://quarry.example.com:8420/mcp",
        "ca_cert": "/path/to/ca.crt",
        "headers": {"Authorization": "Bearer tok"},
    }
    proxy_config = {"quarry": inner_config}
    remote_resp = {"task_id": "xyz", "status": "accepted"}
    with (
        patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
        patch("quarry.__main__._remote_https_request", return_value=remote_resp),
    ):
        result = runner.invoke(app, ["--quiet", "sync", "--workers", "4"])
    _reset_globals()
    assert result.exit_code == 0
    assert "--workers" not in result.stderr
```

Asserts: the `--workers` warning is suppressed with `--quiet`.

---

## Item 4a: `remember_cmd` progress wrapper

**Architecture ref**: Section 6.10

### Current code (lines 850-864)

```python
    settings = _resolved_settings()
    db = get_db(settings.lancedb_path)

    result = ingest_content(
        content,
        name,
        db,
        settings,
        overwrite=overwrite,
        collection=collection,
        format_hint=format_hint,
        agent_handle=agent_handle,
        memory_type=memory_type,
        summary=summary,
    )
```

### Target code

```python
    settings = _resolved_settings()
    db = get_db(settings.lancedb_path)

    with _progress("Remembering") as cb:
        result = ingest_content(
            content,
            name,
            db,
            settings,
            overwrite=overwrite,
            collection=collection,
            format_hint=format_hint,
            progress_callback=cb,
            agent_handle=agent_handle,
            memory_type=memory_type,
            summary=summary,
        )
```

The only changes:

1. Wrap `ingest_content` call in `with _progress("Remembering") as cb:`
2. Add `progress_callback=cb` keyword argument

The `ingest_content` signature already accepts `progress_callback`.
No library change needed.

### Tests

**File**: `tests/test_cli.py`

**Test 18**: `test_remember_passes_progress_callback`

```python
def test_remember_passes_progress_callback():
    """remember_cmd passes a progress callback to ingest_content."""
    _reset_globals()
    cli_mod._json_output = False
    cli_mod._quiet = False
    with (
        patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
        patch("quarry.__main__.get_db"),
        patch(
            "quarry.__main__.ingest_content",
            return_value={"document_name": "n.md", "chunks": 1},
        ) as mock_ingest,
        patch("quarry.__main__.Progress") as MockProgress,
    ):
        mock_instance = MagicMock()
        MockProgress.return_value = mock_instance
        mock_instance.add_task.return_value = 0
        result = runner.invoke(
            app,
            ["remember", "--name", "n.md"],
            input="some content",
        )
    _reset_globals()
    assert result.exit_code == 0
    assert mock_ingest.call_args[1]["progress_callback"] is not None
```

Asserts: `ingest_content` receives a non-None `progress_callback` in
default (non-quiet, non-json) mode.

**Test 19**: `test_remember_quiet_no_progress`

```python
def test_remember_quiet_no_progress():
    """remember_cmd passes None callback to ingest_content in quiet mode."""
    _reset_globals()
    with (
        patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
        patch("quarry.__main__.get_db"),
        patch(
            "quarry.__main__.ingest_content",
            return_value={"document_name": "n.md", "chunks": 1},
        ) as mock_ingest,
    ):
        result = runner.invoke(
            app,
            ["--quiet", "remember", "--name", "n.md"],
            input="some content",
        )
    _reset_globals()
    assert result.exit_code == 0
    assert mock_ingest.call_args[1]["progress_callback"] is None
```

Asserts: `progress_callback` is `None` in quiet mode (no spinner).

---

## Item 4b: `--verbose` help text update

**Architecture ref**: Section 6.11

### Current code (line 163)

```python
        typer.Option("--verbose", "-v", help="Verbose output."),
```

### Target code

```python
        typer.Option(
            "--verbose",
            "-v",
            help="Show INFO-level diagnostic logs on stderr (timing, plans, counts).",
        ),
```

### Tests

**File**: `tests/test_cli.py`

**Test 20**: `test_verbose_help_text_describes_stderr`

```python
def test_verbose_help_text_describes_stderr():
    """--verbose help text mentions stderr and INFO-level logs."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "INFO" in result.output or "stderr" in result.output
```

Asserts: the help output mentions INFO-level or stderr, not the vague
"Verbose output." string.

---

## Item 4c: `QUARRY_LOG_LEVEL` env var support

**Architecture ref**: Section 8.7 (escape hatch for DEBUG)

### Changes to `logging_config.py`

**Current signature** (line 19):

```python
def configure_logging(*, stderr_level: str = "WARNING") -> None:
```

**Target signature**:

```python
def configure_logging(*, stderr_level: str = "WARNING") -> None:
```

Signature unchanged. The env var override happens inside the function body.

**Current body** (lines 25-59): calls `logging.config.dictConfig` with the
`stderr_level` parameter directly.

**Target body**: Before the `dictConfig` call, check `QUARRY_LOG_LEVEL`:

```python
def configure_logging(*, stderr_level: str = "WARNING") -> None:
    """Configure logging with rotating file and stderr handlers.

    File handler is always active at INFO level.
    Stderr handler level is controlled by the caller, unless overridden
    by the ``QUARRY_LOG_LEVEL`` environment variable.
    """
    _LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

    env_level = os.environ.get("QUARRY_LOG_LEVEL", "").upper()
    if env_level and hasattr(logging, env_level):
        effective_level = env_level
    else:
        effective_level = stderr_level

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": _FORMAT,
                    "datefmt": _DATE_FORMAT,
                },
            },
            "handlers": {
                "file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "filename": str(_LOG_FILE),
                    "maxBytes": _MAX_BYTES,
                    "backupCount": _BACKUP_COUNT,
                    "encoding": "utf-8",
                    "formatter": "standard",
                    "level": "INFO",
                },
                "stderr": {
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                    "formatter": "standard",
                    "level": effective_level,
                },
            },
            "loggers": {
                "lancedb": {"level": "WARNING"},
                "onnxruntime": {"level": "WARNING"},
                "httpx": {"level": "WARNING"},
            },
            "root": {
                "level": "DEBUG",
                "handlers": ["file", "stderr"],
            },
        }
    )
```

Changes:

1. Add `import os` to logging_config.py imports.
2. Read `QUARRY_LOG_LEVEL` env var and validate it is a valid logging level.
3. Override `stderr_level` with env var when present.
4. Add `loggers` block to suppress third-party noise at WARNING, preventing
   lancedb/onnxruntime/httpx DEBUG floods even with `QUARRY_LOG_LEVEL=DEBUG`.

### Tests

**File**: `tests/test_logging_config.py` (new file)

**Test 21**: `test_env_var_overrides_stderr_level`

```python
def test_env_var_overrides_stderr_level(monkeypatch: pytest.MonkeyPatch):
    """QUARRY_LOG_LEVEL overrides the stderr_level parameter."""
    monkeypatch.setenv("QUARRY_LOG_LEVEL", "DEBUG")
    with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
        configure_logging(stderr_level="WARNING")
    config = mock_dc.call_args[0][0]
    assert config["handlers"]["stderr"]["level"] == "DEBUG"
```

Asserts: env var `DEBUG` overrides the function parameter `WARNING`.

**Test 22**: `test_invalid_env_var_ignored`

```python
def test_invalid_env_var_ignored(monkeypatch: pytest.MonkeyPatch):
    """Invalid QUARRY_LOG_LEVEL is ignored; parameter value is used."""
    monkeypatch.setenv("QUARRY_LOG_LEVEL", "NONSENSE")
    with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
        configure_logging(stderr_level="WARNING")
    config = mock_dc.call_args[0][0]
    assert config["handlers"]["stderr"]["level"] == "WARNING"
```

Asserts: an invalid level string falls back to the parameter value.

**Test 23**: `test_no_env_var_uses_parameter`

```python
def test_no_env_var_uses_parameter(monkeypatch: pytest.MonkeyPatch):
    """Without QUARRY_LOG_LEVEL, the parameter controls stderr level."""
    monkeypatch.delenv("QUARRY_LOG_LEVEL", raising=False)
    with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
        configure_logging(stderr_level="INFO")
    config = mock_dc.call_args[0][0]
    assert config["handlers"]["stderr"]["level"] == "INFO"
```

Asserts: without the env var, the function parameter is used directly.

**Test 24**: `test_third_party_loggers_suppressed`

```python
def test_third_party_loggers_suppressed(monkeypatch: pytest.MonkeyPatch):
    """Third-party loggers are pinned at WARNING to prevent DEBUG floods."""
    monkeypatch.delenv("QUARRY_LOG_LEVEL", raising=False)
    with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
        configure_logging()
    config = mock_dc.call_args[0][0]
    for name in ("lancedb", "onnxruntime", "httpx"):
        assert config["loggers"][name]["level"] == "WARNING"
```

Asserts: third-party loggers are pinned at WARNING.

---

## Test Summary

All tests go into `tests/test_cli.py` unless otherwise noted (Items 21-24
go into `tests/test_logging_config.py`).

| # | Test function | File | Flag combo | Asserts |
|---|---------------|------|------------|---------|
| 1 | `test_configure_logging_called_in_main_callback` | test_cli.py | (default) | `configure_logging` called with `stderr_level="WARNING"` |
| 2 | `test_configure_logging_not_called_at_import` | test_cli.py | n/a | No module-level `configure_logging` call in AST |
| 3 | `test_progress_uses_stderr_console` | test_cli.py | (default) | `Progress(console=err_console)` |
| 4 | `test_progress_suppressed_in_quiet_mode` | test_cli.py | `--quiet` | callback is `None` |
| 5 | `test_progress_suppressed_in_json_mode` | test_cli.py | `--json` | callback is `None` |
| 6 | `test_progress_yields_callback_in_default_mode` | test_cli.py | (default) | callback is callable |
| 7 | `test_verbose_flag_sets_info_level` | test_cli.py | `--verbose` | `configure_logging(stderr_level="INFO")` |
| 8 | `test_quiet_flag_sets_critical_level` | test_cli.py | `--quiet` | `configure_logging(stderr_level="CRITICAL")` |
| 9 | (existing) | test_cli.py | `--verbose --quiet` | exit code 1 |
| 10 | `test_uninstall_result_on_stdout` | test_cli.py | (default) | `"Service removed."` in `result.stdout` |
| 11 | `test_uninstall_json_mode` | test_cli.py | `--json` | JSON with `"message"` key in stdout |
| 12 | `test_login_abort_message_on_stderr` | test_cli.py | (default) | `"Aborted"` in stderr, not in stdout |
| 13 | `test_no_stdout_console_variable` | test_cli.py | n/a | `cli_mod.console` does not exist |
| 14 | `test_sync_409_quiet_suppresses_warning` | test_cli.py | `--quiet` | no `"Sync already in progress"` in stderr |
| 15 | `test_sync_409_default_shows_warning` | test_cli.py | (default) | `"Sync already in progress"` in stderr |
| 16 | `test_login_fingerprint_quiet_suppressed` | test_cli.py | `--quiet` | no `"fingerprint"` in stderr |
| 17 | `test_sync_workers_warning_quiet_suppressed` | test_cli.py | `--quiet` | no `"--workers"` in stderr |
| 18 | `test_remember_passes_progress_callback` | test_cli.py | (default) | `progress_callback` is not None |
| 19 | `test_remember_quiet_no_progress` | test_cli.py | `--quiet` | `progress_callback` is None |
| 20 | `test_verbose_help_text_describes_stderr` | test_cli.py | `--help` | output mentions INFO or stderr |
| 21 | `test_env_var_overrides_stderr_level` | test_logging_config.py | env `DEBUG` | stderr handler level is `"DEBUG"` |
| 22 | `test_invalid_env_var_ignored` | test_logging_config.py | env `NONSENSE` | stderr handler level is `"WARNING"` |
| 23 | `test_no_env_var_uses_parameter` | test_logging_config.py | no env | stderr handler level matches parameter |
| 24 | `test_third_party_loggers_suppressed` | test_logging_config.py | (default) | lancedb/onnxruntime/httpx at WARNING |

---

## Files Modified

| File | Changes |
|------|---------|
| `src/quarry/__main__.py` | Delete line 78 `configure_logging(...)`. Add `configure_logging` call in `main_callback`. Change `_progress` to use `err_console` and `_quiet` guard. Change `uninstall_cmd` to use `_emit`. Change `login_cmd` abort to `err_console.print`. Delete `console = Console()`. Add quiet guards to 4 remote-path locations. Wrap `remember_cmd` `ingest_content` in `_progress`. Update `--verbose` help text. |
| `src/quarry/logging_config.py` | Add `import os`. Read `QUARRY_LOG_LEVEL` env var. Add third-party logger suppression in `loggers` block. |
| `tests/test_cli.py` | Add 19 new test functions (Tests 1-8, 10-20). |
| `tests/test_logging_config.py` | New file with 4 tests (Tests 21-24). |

---

## Rejected During Design

**Moving `err_console.print` error messages under `if not _quiet`.**
Error messages that precede `raise typer.Exit(code=1)` are always shown
per the quiet contract (Section 4.3 of the architecture doc). Only
non-fatal informational messages and warnings get quiet guards. The
`_cli_errors` decorator's error printing is also always-on, which is
correct -- a failing command should always explain why, even in quiet mode.

**Adding a `--debug` flag.** Rejected per architecture doc Section 8.2.
The `QUARRY_LOG_LEVEL` env var is the escape hatch.

**Changing `_safe_proxy_config` warning to respect `--quiet`.** The
`_safe_proxy_config` warning at line 393 fires only on malformed TOML.
This is a degraded condition (the proxy config is broken), not a routine
informational message. It should always be visible. No quiet guard.
