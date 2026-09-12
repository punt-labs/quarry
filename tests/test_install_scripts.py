"""Shell-integration tests for install.sh.

These tests invoke install.sh with different flags (no flag, --network,
--no-plugin) and environment variables using a ``PATH`` pointing at mock
versions of ``nvidia-smi``, ``uv``, ``curl``, ``ssh``, ``claude``, and
``quarry``.  Each mock records its invocation to a log file; the tests then
read the log and assert the expected call ordering.

The installer has one daemon-bind mode selector and one plugin toggle:

  - **Default** (no flags): full install -- daemon on localhost, TLS,
    plugin (if claude CLI found), local quarry login.
  - **--network**: same as default, but binds daemon to 0.0.0.0 instead
    of localhost.  Requires QUARRY_API_KEY.
  - **--no-plugin** / **QUARRY_NO_PLUGIN=1**: install the harness-neutral CLI
    but skip the Claude Code marketplace-register + plugin-install steps.  For
    non-Claude harnesses and enterprise-policy Claude users (claude present,
    marketplace blocked).  See punt-kit/standards/install-cli-only.md.

Ordering + scoping invariants asserted per CLAUDE.md Class 5:

  (a) ``uv tool install --force`` runs before any ``uv pip`` GPU swap call
  (b) When ``nvidia-smi`` reports an NVIDIA GPU, the GPU swap uninstalls
      ``onnxruntime`` and installs ``onnxruntime-gpu`` *before* ``quarry install``
  (c) When ``nvidia-smi`` is absent, the GPU swap is not invoked at all
  (d) ``quarry install`` runs after the GPU swap (where applicable)
  (e) ``--network`` without ``QUARRY_API_KEY`` fails early
  (f) Unknown flags exit 2 with a usage string (a piped installer must not
      silently ignore a misspelled ``--no-plguin``)
  (g) ``sh -s -- --network`` / ``sh -s -- --no-plugin`` / ``QUARRY_NO_PLUGIN=1
      sh`` all work (POSIX piped-stdin argument + environment passing)
  (h) Plugin install is skipped (no failure) when claude CLI is absent, and the
      auto-skip prints the same CLI-only message as an explicit --no-plugin skip
  (i) Plugin install runs when claude CLI is present and no skip was requested
  (j) --no-plugin / QUARRY_NO_PLUGIN=1 skip ONLY the marketplace + plugin steps;
      the binary, model+TLS install, local login, and health check still run
  (k) QUARRY_NO_PLUGIN is honored only when exactly ``1`` (no truthy parser)
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"


def _write_mock(path: Path, body: str) -> None:
    """Create an executable shell-script mock at ``path``."""
    path.write_text("#!/bin/sh\n" + dedent(body).lstrip())
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def mock_bin(tmp_path: Path) -> Path:
    """Mock ``bin`` directory with stubs for every external command the
    install script invokes.

    Each mock appends one line per invocation to ``$LOG_FILE``: the mock
    name followed by its argv, space-separated, so tests can assert on call
    ordering.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # Default: mocks record their invocation and exit 0.
    log_header = (
        'printf "%s" "$(basename "$0")" >> "$LOG_FILE"\n'
        'for a in "$@"; do printf " %s" "$a" >> "$LOG_FILE"; done\n'
        'printf "\\n" >> "$LOG_FILE"\n'
    )

    # git -- prerequisite check only.
    _write_mock(bin_dir / "git", log_header + "exit 0\n")
    _write_mock(
        bin_dir / "python3",
        log_header
        + 'if [ "$1" = "-c" ]; then\n'
        + '  case "$2" in\n'
        + '    *major*) printf "3\\n"; exit 0 ;;\n'
        + '    *minor*) printf "13\\n"; exit 0 ;;\n'
        + "  esac\n"
        + "fi\n"
        + "exit 0\n",
    )

    # claude -- marketplace/plugin commands.
    _write_mock(
        bin_dir / "claude",
        log_header
        + 'case "$1" in\n'
        + "  plugin)\n"
        + '    case "$2" in\n'
        + '      marketplace) [ "$3" = "list" ] && printf "punt-labs\\n"; exit 0 ;;\n'
        + '      list) printf "quarry@punt-labs\\n"; exit 0 ;;\n'
        + "    esac\n"
        + "    exit 0 ;;\n"
        + "esac\n"
        + "exit 0\n",
    )

    # uv -- subcommands used by the script.  When `--overrides FILE` is present
    # (the opencv-headless override on `uv tool install`), dump FILE's contents
    # as `uv-override <line>` so tests can assert what was passed through.
    _write_mock(
        bin_dir / "uv",
        log_header
        + 'prev=""\n'
        + 'for a in "$@"; do\n'
        + '  if [ "$prev" = "--overrides" ] && [ -f "$a" ]; then\n'
        + "    while IFS= read -r ovl; do"
        + ' printf "uv-override %s\\n" "$ovl" >> "$LOG_FILE"; done < "$a"\n'
        + "  fi\n"
        + '  prev="$a"\n'
        + "done\n"
        + "exit 0\n",
    )

    # quarry -- the install script does two things with ``quarry``:
    #   1. ``command -v quarry`` + ``head -1 ... | sed 's/^#!//'`` to extract
    #      the tool venv's Python interpreter path from the shebang.
    #   2. ``"$BINARY" install`` / ``"$BINARY" login`` / ``"$BINARY" doctor``
    #      to actually invoke the CLI.
    #
    # Real uv-tool-installed quarry is ``#!/path/to/tool-venv/bin/python\n``
    # followed by Python bytecode.  When sh executes ``quarry``, the kernel
    # runs ``python quarry <argv>``.  Our mock replicates this: the shebang
    # points at ``fake-tool-python``, a shell script that execs ``sh`` on
    # its second argument (the quarry path).  The quarry mock body itself
    # logs the invocation.
    quarry_path = bin_dir / "quarry"
    fake_tool_python = bin_dir / "fake-tool-python"
    _write_mock(
        fake_tool_python,
        'exec /bin/sh "$@"\n',
    )
    quarry_path.write_text(
        f"#!{fake_tool_python}\n" + log_header + "exit 0\n",
    )
    quarry_path.chmod(
        quarry_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )

    # curl -- used for health checks.  Emit the ready health body so the
    # install gate (which now requires state=="ready", not a bare 200)
    # matches and the loop terminates fast.
    _write_mock(
        bin_dir / "curl",
        log_header + 'printf \'{"state":"ready"}\\n\'\nexit 0\n',
    )

    # ssh -- the script tests SSH to github.com for HTTPS fallback.
    # Return a success banner so the HTTPS rewrite is skipped.
    _write_mock(
        bin_dir / "ssh",
        log_header
        + 'printf "Hi there! You successfully authenticated.\\n" >&2\n'
        + "exit 0\n",
    )

    # systemctl / launchctl -- used by the belt-and-suspenders restart block.
    _write_mock(bin_dir / "systemctl", log_header + "exit 0\n")
    _write_mock(bin_dir / "launchctl", log_header + "exit 0\n")

    # head / sed / id -- real utilities from the host; we add them to the
    # mock PATH so scripts don't pick up something unexpected.  Symlink to
    # the real binaries.
    for util in (
        "head",
        "sed",
        "id",
        "printf",
        "sleep",
        "grep",
        "basename",
        # mktemp + rm back install.sh's uv-overrides temp file (the opencv
        # headless override written before `uv tool install`).
        "mktemp",
        "rm",
    ):
        real = shutil.which(util)
        if real is not None:
            (bin_dir / util).symlink_to(real)

    # openssl -- used in the success message but not critical.
    _write_mock(
        bin_dir / "openssl",
        log_header + 'printf "abcdef1234567890\\n"\nexit 0\n',
    )

    return bin_dir


@pytest.fixture
def env(mock_bin: Path, tmp_path: Path) -> dict[str, str]:
    """Clean environment pointing at ``mock_bin`` and a per-test LOG_FILE."""
    log = tmp_path / "calls.log"
    log.touch()
    # PATH intentionally excludes /usr/bin and /bin: the mock bin directory
    # contains symlinks to real utilities the scripts need (head, sed, id,
    # grep, basename, printf, sleep) and mocks for everything else.  If we
    # inherit the host PATH, a real ``nvidia-smi`` on the test host bypasses
    # the "no GPU detected" branch and the CPU-only test fails.
    return {
        "PATH": str(mock_bin),
        "HOME": str(tmp_path),
        "LOG_FILE": str(log),
        # Prevent set -u errors on QUARRY_API_KEY when scripts source env.
        "QUARRY_API_KEY": "test-key-not-used",
        # shell is launched as /bin/sh by the install scripts.
        "SHELL": "/bin/sh",
    }


def _run_script(
    script: Path,
    env: dict[str, str],
    *,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` under /bin/sh with ``env`` and return the result.

    Scripts use ``set -eu`` so any mock stub that exits non-zero will abort
    the run.  Use ``check=False`` because some tests want to inspect the
    exit code.
    """
    cmd = ["/bin/sh", str(script)]
    if args:
        cmd.extend(args)
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(script.parent),
    )


def _run_script_piped(
    script: Path,
    env: dict[str, str],
    *,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` via ``sh -s -- <args>`` with stdin piped from the script.

    This simulates ``curl ... | sh -s -- --network`` which is the POSIX way
    to pass arguments when piping to sh.
    """
    cmd = ["/bin/sh", "-s"]
    if args:
        cmd.append("--")
        cmd.extend(args)
    return subprocess.run(
        cmd,
        input=script.read_text(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(script.parent),
    )


def _read_log(env: dict[str, str]) -> list[str]:
    return Path(env["LOG_FILE"]).read_text().splitlines()


def _index_of(log: list[str], needle: str) -> int:
    """Return the index of the first log line containing ``needle``.

    Raises ``AssertionError`` with the full log on miss so failure output
    tells the reader what the mock actually saw.
    """
    for i, line in enumerate(log):
        if needle in line:
            return i
    formatted = "\n".join(f"  {i}: {line}" for i, line in enumerate(log))
    raise AssertionError(
        f"Expected call not found in log:\n  needle={needle!r}\n\nLog:\n{formatted}"
    )


def _any_line_contains(log: list[str], needle: str) -> bool:
    return any(needle in line for line in log)


def _any_command_invoked(log: list[str], cmd: str) -> bool:
    """Return True iff any log line records an invocation of ``cmd``.

    Substring matches on bare command names (e.g. ``"claude"``) false-
    positive on path fragments — a ``curl --cacert /home/u/.claude/...``
    line contains ``"claude"`` but is not a ``claude`` invocation.  The
    mock recorder writes ``<basename> <args...>\\n``, so a real invocation
    starts with the command followed by either a space or the end of the
    line.
    """
    return any(line.startswith(f"{cmd} ") or line == cmd for line in log)


# ---------------------------------------------------------------------------
# Default mode (full install, with claude CLI present)
# ---------------------------------------------------------------------------


def test_default_mode_gpu_swap_runs_before_quarry_install(
    env: dict[str, str], mock_bin: Path
) -> None:
    """Default mode with GPU: GPU swap MUST run before ``quarry install``."""
    _write_mock(
        mock_bin / "nvidia-smi",
        'printf "%s" "$(basename "$0")" >> "$LOG_FILE"\n'
        'for a in "$@"; do printf " %s" "$a" >> "$LOG_FILE"; done\n'
        'printf "\\n" >> "$LOG_FILE"\n'
        "exit 0\n",
    )

    result = _run_script(INSTALL_SH, env)
    assert result.returncode == 0, (
        f"install.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    log = _read_log(env)

    tool_install_idx = _index_of(log, "uv tool install --force")
    gpu_install_idx = _index_of(log, "onnxruntime-gpu")
    quarry_install_idx = _index_of(log, "quarry install")

    assert tool_install_idx < gpu_install_idx < quarry_install_idx


def test_default_mode_runs_quarry_install(env: dict[str, str]) -> None:
    """Default mode always runs ``quarry install`` (localhost)."""
    result = _run_script(INSTALL_SH, env)
    assert result.returncode == 0, (
        f"install.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    log = _read_log(env)
    _index_of(log, "quarry install")


def test_default_mode_runs_plugin_install_with_claude(env: dict[str, str]) -> None:
    """Default mode with claude CLI installs the plugin."""
    result = _run_script(INSTALL_SH, env)
    assert result.returncode == 0, (
        f"install.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    log = _read_log(env)
    assert _any_line_contains(log, "claude plugin install"), (
        "Default mode with claude CLI must install the plugin"
    )


def test_default_mode_runs_quarry_login(env: dict[str, str]) -> None:
    """Default mode runs ``quarry login 127.0.0.1`` — the literal the daemon
    binds, never the ambiguous ``localhost`` name (HIGH: token-presentation is
    gated on a literal loopback IP)."""
    result = _run_script(INSTALL_SH, env)
    assert result.returncode == 0, (
        f"install.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    log = _read_log(env)
    assert _any_line_contains(log, "login 127.0.0.1"), (
        "Default mode must run `quarry login 127.0.0.1` (the literal loopback IP), "
        "not the ambiguous localhost name"
    )


# ---------------------------------------------------------------------------
# Default mode without claude CLI
# ---------------------------------------------------------------------------


def test_default_mode_no_claude_skips_plugin(
    env: dict[str, str], mock_bin: Path
) -> None:
    """(h) When claude CLI is absent, plugin install is skipped without failure."""
    # Remove the claude mock so command -v claude fails.
    (mock_bin / "claude").unlink()

    result = _run_script(INSTALL_SH, env)
    assert result.returncode == 0, (
        f"install.sh failed without claude:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    log = _read_log(env)
    assert not _any_command_invoked(log, "claude"), (
        "Plugin install must be skipped when claude CLI is absent"
    )

    # quarry install still runs (daemon on localhost).
    _index_of(log, "quarry install")

    # Success message should mention Claude Code not found.
    assert "Claude Code" in result.stdout or "not found" in result.stdout

    # install-cli-only.md: the capability-absent auto-skip prints the SAME
    # CLI-only block as an explicit --no-plugin skip — gated on the skip boolean,
    # not the reason. It must NOT claim a plugin was activated when none was
    # installed (the common bug this rule prevents).
    assert "Restart Claude Code to activate the plugin" not in result.stdout, (
        "auto-skip must not print the plugin-activation line — no plugin was installed"
    )
    assert "CLI is installed and works" in result.stdout, (
        "auto-skip must state the CLI is installed and works"
    )


# ---------------------------------------------------------------------------
# --network mode
# ---------------------------------------------------------------------------


def test_network_mode_gpu_swap_runs_before_quarry_install(
    env: dict[str, str], mock_bin: Path
) -> None:
    """--network mode: the shell-level GPU swap MUST run before
    ``quarry install`` when an NVIDIA GPU is present.
    """
    _write_mock(
        mock_bin / "nvidia-smi",
        'printf "%s" "$(basename "$0")" >> "$LOG_FILE"\n'
        'for a in "$@"; do printf " %s" "$a" >> "$LOG_FILE"; done\n'
        'printf "\\n" >> "$LOG_FILE"\n'
        "exit 0\n",
    )

    result = _run_script(INSTALL_SH, env, args=["--network"])
    assert result.returncode == 0, (
        f"install.sh --network failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    log = _read_log(env)

    # (a) uv tool install --force runs before any GPU swap.
    tool_install_idx = _index_of(log, "uv tool install --force")
    uninstall_idx = _index_of(log, "uv pip uninstall")
    gpu_install_idx = _index_of(log, "uv pip install")
    assert tool_install_idx < uninstall_idx, (
        "uv tool install --force must come before uv pip uninstall onnxruntime"
    )
    assert uninstall_idx < gpu_install_idx, (
        "uv pip uninstall onnxruntime must come before uv pip install onnxruntime-gpu"
    )

    # (b) GPU swap installs onnxruntime-gpu specifically.
    gpu_install_line = log[gpu_install_idx]
    assert "onnxruntime-gpu" in gpu_install_line, (
        f"Expected uv pip install to install onnxruntime-gpu, saw: {gpu_install_line}"
    )

    # (d) quarry install runs AFTER the GPU swap.
    quarry_install_idx = _index_of(log, "quarry install")
    assert gpu_install_idx < quarry_install_idx, (
        "GPU swap must run before quarry install so the service daemon "
        "starts with CUDA providers available"
    )


def test_network_mode_skips_gpu_swap_without_nvidia(
    env: dict[str, str], mock_bin: Path
) -> None:
    """--network mode: No nvidia-smi on PATH -> no GPU swap."""
    assert not (mock_bin / "nvidia-smi").exists()

    result = _run_script(INSTALL_SH, env, args=["--network"])
    assert result.returncode == 0, (
        f"install.sh --network failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    log = _read_log(env)

    # (c) No GPU swap calls at all.
    assert not _any_line_contains(log, "uv pip uninstall"), (
        "GPU swap must not run when nvidia-smi is absent"
    )
    assert not _any_line_contains(log, "onnxruntime-gpu"), (
        "onnxruntime-gpu must not be installed on CPU-only hosts"
    )

    # quarry install still runs.
    _index_of(log, "quarry install")


def test_network_mode_fails_without_quarry_api_key(
    env: dict[str, str],
) -> None:
    """(e) --network without QUARRY_API_KEY must fail early."""
    env_no_key = {**env}
    del env_no_key["QUARRY_API_KEY"]

    result = _run_script(INSTALL_SH, env_no_key, args=["--network"])
    assert result.returncode != 0, "--network without QUARRY_API_KEY must exit non-zero"
    assert "QUARRY_API_KEY" in result.stdout, (
        "Error message must mention QUARRY_API_KEY"
    )


def test_network_mode_fails_when_daemon_never_becomes_ready(
    env: dict[str, str], mock_bin: Path
) -> None:
    """(Class 5) --network install exits non-zero when the daemon never readies.

    The health gate must fail CLOSED: a daemon stuck warming (``/health`` keeps
    returning ``state=="starting"``) must drain the retry budget and exit
    non-zero with an actionable message — never hang, never green-light an
    unready daemon. ``curl`` is mocked to always return the warming body; the
    ``sleep`` symlink is replaced with an instant no-op so the loop drains fast.
    """
    # /health always reports warming, never ready.
    _write_mock(
        mock_bin / "curl",
        'printf \'{"state":"starting"}\\n\'\nexit 0\n',
    )
    # Replace the real-sleep symlink with an instant no-op so the retry budget
    # drains in milliseconds (write_text would follow the symlink to /bin/sleep).
    (mock_bin / "sleep").unlink()
    _write_mock(mock_bin / "sleep", "exit 0\n")

    result = _run_script(INSTALL_SH, env, args=["--network"])
    assert result.returncode != 0, (
        "install must exit non-zero when the daemon never becomes ready"
    )
    assert "did not become healthy" in result.stdout, (
        "the failure must carry the actionable 'did not become healthy' message"
    )


def test_network_mode_runs_quarry_install(env: dict[str, str]) -> None:
    """--network mode runs ``quarry install`` with QUARRY_SERVE_HOST=0.0.0.0."""
    result = _run_script(INSTALL_SH, env, args=["--network"])
    assert result.returncode == 0, (
        f"install.sh --network failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    log = _read_log(env)
    _index_of(log, "quarry install")


def test_network_mode_installs_plugin_with_claude(env: dict[str, str]) -> None:
    """--network mode with claude CLI still installs the plugin."""
    result = _run_script(INSTALL_SH, env, args=["--network"])
    assert result.returncode == 0, (
        f"install.sh --network failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    log = _read_log(env)
    assert _any_line_contains(log, "claude plugin install"), (
        "--network mode with claude CLI must install the plugin"
    )


def test_network_mode_success_message(env: dict[str, str]) -> None:
    """--network mode success message includes remote connection instructions."""
    result = _run_script(INSTALL_SH, env, args=["--network"])
    assert result.returncode == 0

    assert "server is ready" in result.stdout
    assert "quarry login" in result.stdout


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_unknown_flag_fails(env: dict[str, str]) -> None:
    """(f) Unknown flags must exit 2 with a usage string.

    Per install-cli-only.md: a piped ``curl … | sh`` must not silently ignore a
    misspelled flag (``--no-plguin``) and install the plugin the user asked to
    skip. An unknown option is a usage error with the POSIX conventional exit 2.
    """
    result = _run_script(INSTALL_SH, env, args=["--bogus"])
    assert result.returncode == 2, "Unknown flag must exit 2 (usage error)"
    assert "unknown option" in result.stderr, (
        "Error message must name the unknown option"
    )
    # The usage string is printed to stderr alongside the error.
    assert "--no-plugin" in result.stderr, "usage must document --no-plugin"


def test_help_flag_exits_zero(env: dict[str, str]) -> None:
    """--help must exit 0 and print usage."""
    result = _run_script(INSTALL_SH, env, args=["--help"])
    assert result.returncode == 0
    assert "--network" in result.stdout


def test_help_does_not_mention_old_flags(env: dict[str, str]) -> None:
    """--help must not mention removed --server or --client flags."""
    result = _run_script(INSTALL_SH, env, args=["--help"])
    assert result.returncode == 0
    assert "--server" not in result.stdout
    assert "--client" not in result.stdout


def test_old_flags_fail(env: dict[str, str]) -> None:
    """Removed --server and --client flags must fail as unknown options (exit 2)."""
    for flag in ("--server", "--client"):
        result = _run_script(INSTALL_SH, env, args=[flag])
        assert result.returncode == 2, f"{flag} must exit 2 (usage error)"
        assert "unknown option" in result.stderr, (
            f"{flag} must be reported as an unknown option"
        )


# ---------------------------------------------------------------------------
# --no-plugin / QUARRY_NO_PLUGIN=1 (install-cli-only.md conformance)
#
# The installer does two jobs in one run: it installs a harness-neutral CLI
# (binary, PATH, model, TLS, per-repo enable, health check) and registers a
# Claude-Code-only plugin (marketplace add, claude plugin install). Per
# punt-kit/standards/install-cli-only.md, an operator with claude present but
# marketplace blocked (or a non-Claude harness user) must be able to skip ONLY
# the plugin steps. These tests hold that line: the flag and env forms skip the
# marketplace + plugin steps and nothing else.
# ---------------------------------------------------------------------------


def _assert_plugin_skipped_cli_intact(
    result: subprocess.CompletedProcess[str], log: list[str]
) -> None:
    """Assert the plugin was skipped while every CLI step still ran.

    Shared by the flag, env, and piped-form tests so the scoping invariant is
    expressed once. ``claude`` is only logged when the plugin steps run, so its
    total absence proves both the marketplace-register and plugin-install steps
    were skipped.
    """
    assert result.returncode == 0, (
        f"install.sh must succeed with the plugin skipped:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    # Skip is scoped to marketplace + plugin ONLY: no claude call of any kind.
    assert not _any_line_contains(log, "claude plugin"), (
        "marketplace-register and plugin-install steps must be skipped"
    )
    assert not _any_command_invoked(log, "claude"), (
        "no claude invocation at all when the plugin is skipped"
    )
    # Every CLI step still runs: model+TLS install, local login, health check.
    _index_of(log, "quarry install")
    assert _any_line_contains(log, "login 127.0.0.1"), (
        "per-repo local login must still run under --no-plugin"
    )
    assert _any_line_contains(log, "quarry doctor"), (
        "the health check (quarry doctor) must still run under --no-plugin"
    )
    # Success message is CLI-only accurate: never claims a plugin was activated.
    assert "Restart Claude Code to activate the plugin" not in result.stdout, (
        "skip message must not print the plugin-activation line"
    )
    assert "CLI is installed and works" in result.stdout, (
        "skip message must state the CLI is installed and works"
    )


def test_no_plugin_flag_skips_plugin_only(env: dict[str, str]) -> None:
    """(a) --no-plugin skips the marketplace + plugin steps and nothing else."""
    result = _run_script(INSTALL_SH, env, args=["--no-plugin"])
    _assert_plugin_skipped_cli_intact(result, _read_log(env))


def test_no_plugin_flag_remedy_names_the_flag(env: dict[str, str]) -> None:
    """An explicit --no-plugin skip tells the operator how to add the plugin later.

    The remedy branches on the cause: a requested skip re-runs without the flag
    (and with QUARRY_NO_PLUGIN unset), never "install claude" — claude is present.
    """
    result = _run_script(INSTALL_SH, env, args=["--no-plugin"])
    assert result.returncode == 0
    assert "--no-plugin" in result.stdout, (
        "requested-skip remedy must name the --no-plugin flag to re-enable the plugin"
    )


def test_quarry_no_plugin_env_skips_identically(env: dict[str, str]) -> None:
    """(b) QUARRY_NO_PLUGIN=1 behaves identically to --no-plugin, no flag needed."""
    env_skip = {**env, "QUARRY_NO_PLUGIN": "1"}
    result = _run_script(INSTALL_SH, env_skip)
    _assert_plugin_skipped_cli_intact(result, _read_log(env_skip))


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "2"])
def test_quarry_no_plugin_env_ignored_unless_exactly_one(
    env: dict[str, str], value: str
) -> None:
    """QUARRY_NO_PLUGIN is honored ONLY when set to exactly ``1``.

    install-cli-only.md forbids a truthy-string parser: empty/0/true/yes must be
    ignored so the value stays consistent with the installer's 0/1 convention.
    With the value ignored, the default happy path installs the plugin.
    """
    env_val = {**env, "QUARRY_NO_PLUGIN": value}
    result = _run_script(INSTALL_SH, env_val)
    assert result.returncode == 0, (
        f"QUARRY_NO_PLUGIN={value!r} run failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    log = _read_log(env_val)
    assert _any_line_contains(log, "claude plugin install"), (
        f"QUARRY_NO_PLUGIN={value!r} must be IGNORED (only '1' skips) — "
        "the plugin must still install"
    )


def test_no_plugin_over_piped_flag_form(env: dict[str, str]) -> None:
    """(c) ``sh -s -- --no-plugin`` skips over a piped curl|sh invocation."""
    result = _run_script_piped(INSTALL_SH, env, args=["--no-plugin"])
    _assert_plugin_skipped_cli_intact(result, _read_log(env))


def test_no_plugin_over_piped_env_form(env: dict[str, str]) -> None:
    """(c) ``QUARRY_NO_PLUGIN=1 sh`` skips over a piped curl|sh invocation.

    The env form is the argument-hostile path: no operands reach the script, the
    skip is driven entirely by the environment the piped shell inherits.
    """
    env_skip = {**env, "QUARRY_NO_PLUGIN": "1"}
    result = _run_script_piped(INSTALL_SH, env_skip)
    _assert_plugin_skipped_cli_intact(result, _read_log(env_skip))


def test_no_plugin_with_network_still_skips_plugin(env: dict[str, str]) -> None:
    """--no-plugin composes with --network: the daemon binds 0.0.0.0, plugin skipped.

    The two flags are orthogonal — one selects the daemon bind host, the other
    gates the plugin steps. The success block is network-specific (server ready)
    and never mentions the plugin, so no restart line appears either way.
    """
    result = _run_script(INSTALL_SH, env, args=["--network", "--no-plugin"])
    assert result.returncode == 0, (
        f"--network --no-plugin failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    log = _read_log(env)
    assert not _any_command_invoked(log, "claude"), (
        "--no-plugin must skip the plugin even in network mode"
    )
    _index_of(log, "quarry install")
    assert "server is ready" in result.stdout


# ---------------------------------------------------------------------------
# sh -s -- --network (piped stdin argument passing)
# ---------------------------------------------------------------------------


def test_piped_network_mode_parses_flag(env: dict[str, str], mock_bin: Path) -> None:
    """(g) ``sh -s -- --network`` must correctly parse the --network flag
    when the script is piped via stdin (simulating curl | sh -s -- --network).
    """
    result = _run_script_piped(INSTALL_SH, env, args=["--network"])
    assert result.returncode == 0, (
        f"sh -s -- --network failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    log = _read_log(env)

    # Verify network-mode behavior: quarry install runs.
    _index_of(log, "quarry install")

    # Success message is network-mode specific.
    assert "server is ready" in result.stdout


def test_piped_default_mode(env: dict[str, str], mock_bin: Path) -> None:
    """Piped default mode (no flags) runs quarry install and plugin."""
    result = _run_script_piped(INSTALL_SH, env)
    assert result.returncode == 0, (
        f"sh -s -- (default) failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    log = _read_log(env)
    _index_of(log, "quarry install")
    assert _any_line_contains(log, "claude plugin install")


# ---------------------------------------------------------------------------
# Shellcheck
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Plugin uninstall error handling
# ---------------------------------------------------------------------------


def test_uninstall_suppresses_not_installed_error(
    env: dict[str, str], mock_bin: Path
) -> None:
    """Plugin uninstall silently suppresses 'not installed' errors."""
    # Replace claude mock: uninstall fails with "not installed", everything
    # else works normally.
    _write_mock(
        mock_bin / "claude",
        'printf "%s" "$(basename "$0")" >> "$LOG_FILE"\n'
        'for a in "$@"; do printf " %s" "$a" >> "$LOG_FILE"; done\n'
        'printf "\\n" >> "$LOG_FILE"\n'
        'case "$1" in\n'
        "  plugin)\n"
        '    case "$2" in\n'
        '      marketplace) [ "$3" = "list" ] && printf "punt-labs\\n"; exit 0 ;;\n'
        '      uninstall) printf "Plugin not installed\\n" >&2; exit 1 ;;\n'
        '      list) printf "quarry@punt-labs\\n"; exit 0 ;;\n'
        "    esac\n"
        "    exit 0 ;;\n"
        "esac\n"
        "exit 0\n",
    )

    result = _run_script(INSTALL_SH, env)
    assert result.returncode == 0, (
        f"install.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # "not installed" is silently suppressed — no warning in output.
    assert "Plugin uninstall failed" not in result.stdout


def test_uninstall_warns_on_unexpected_error(
    env: dict[str, str], mock_bin: Path
) -> None:
    """Plugin uninstall warns (but continues) on unexpected errors."""
    _write_mock(
        mock_bin / "claude",
        'printf "%s" "$(basename "$0")" >> "$LOG_FILE"\n'
        'for a in "$@"; do printf " %s" "$a" >> "$LOG_FILE"; done\n'
        'printf "\\n" >> "$LOG_FILE"\n'
        'case "$1" in\n'
        "  plugin)\n"
        '    case "$2" in\n'
        '      marketplace) [ "$3" = "list" ] && printf "punt-labs\\n"; exit 0 ;;\n'
        '      uninstall) printf "permission denied\\n"; exit 1 ;;\n'
        '      list) printf "quarry@punt-labs\\n"; exit 0 ;;\n'
        "    esac\n"
        "    exit 0 ;;\n"
        "esac\n"
        "exit 0\n",
    )

    result = _run_script(INSTALL_SH, env)
    assert result.returncode == 0, (
        f"install.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # Unexpected error emits a warning.
    assert "Plugin uninstall failed" in result.stdout


# ---------------------------------------------------------------------------
# Shellcheck
# ---------------------------------------------------------------------------


def test_install_script_passes_shellcheck() -> None:
    """Per CLAUDE.md Class 5: install.sh must pass ``shellcheck -x``."""
    shellcheck_bin = shutil.which("shellcheck")
    if shellcheck_bin is None:
        pytest.fail(
            "shellcheck is required for install-script linting "
            "but was not found on PATH. Install shellcheck in "
            "CI (apt-get install shellcheck) so this gate "
            "cannot be skipped."
        )
    result = subprocess.run(
        [shellcheck_bin, "-x", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"shellcheck failed on install.sh:\n{result.stdout}\n{result.stderr}"
    )


def test_install_health_gate_requires_ready_state() -> None:
    """Per CLAUDE.md Class 5: the health gate must check state=="ready".

    A warming daemon returns HTTP 200 with state=="starting", so gating on a
    bare 200 (``curl ... >/dev/null``) would green-light an unready daemon.
    Both health-check loops (network + default) must grep the ready state.
    """
    script = INSTALL_SH.read_text()
    ready_gate = '"state"[[:space:]]*:[[:space:]]*"ready"'
    assert script.count(ready_gate) == 2, "both health loops must gate on state==ready"
    assert '/health" >/dev/null 2>&1; then' not in script, (
        "health check must not gate on a bare HTTP 200"
    )


def test_install_health_gate_probes_literal_loopback_not_localhost() -> None:
    """The health gate must probe the literal 127.0.0.1, not "localhost".

    The daemon binds IPv4 loopback and login pins 127.0.0.1; on an IPv6-preferring
    host "localhost" resolves ::1 first, so a localhost health probe would miss
    the ready IPv4 daemon (false timeout) and reopen the dual-stack ambiguity the
    literal-IP pinning closed.  Both health loops (network + default) must target
    127.0.0.1:8420/health, and no daemon probe may hit localhost:8420.
    """
    script = INSTALL_SH.read_text()
    assert script.count("https://127.0.0.1:8420/health") == 2, (
        "both health loops must probe the literal 127.0.0.1, not localhost"
    )
    assert "localhost:8420" not in script, (
        "no daemon probe may target the dual-stack-ambiguous localhost:8420"
    )


def test_install_health_gate_discriminates_ready_from_starting() -> None:
    """The gate's ready-state pattern matches a ready body and rejects a starting
    one.  Expressed in Python `re` (no external `grep` on PATH): the install
    script's POSIX ``[[:space:]]`` becomes ``\\s``, semantics unchanged."""
    pattern = r'"state"\s*:\s*"ready"'

    def _matches(body: str) -> bool:
        return re.search(pattern, body) is not None

    assert _matches('{"state":"ready"}')
    assert _matches('{"state": "ready", "version": "1.19.0"}')
    assert not _matches('{"state":"starting"}')
    assert not _matches('{"status":"ok"}')


# ---------------------------------------------------------------------------
# opencv-headless override + QUARRY_LOCAL_WHEEL (clean-machine install path)
# ---------------------------------------------------------------------------


def test_opencv_headless_override_passed_to_uv_tool_install(
    env: dict[str, str],
) -> None:
    """install.sh drops the GUI ``opencv-python`` via a uv override.

    rapidocr (a transitive dep) declares the full ``opencv-python``, whose GUI
    build dynamically links X11/GL libraries (libGL.so.1, libxcb.so.1) that
    headless servers and minimal containers lack.  It ships the same ``cv2``
    module as quarry's pinned ``opencv-python-headless`` and shadows it, so
    ``import cv2`` then fails to load -- which makes ``quarry install`` /
    ``quarry doctor`` report required-check failures and aborts the installer
    under ``set -e``.  The installer writes a uv overrides file with a
    never-matching marker so the GUI build is never resolved.
    """
    result = _run_script(INSTALL_SH, env)
    assert result.returncode == 0, (
        f"install.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    log = _read_log(env)
    _index_of(log, "uv tool install --force --overrides")
    assert _any_line_contains(log, "uv-override opencv-python"), (
        "the override file must name opencv-python (the GUI build to drop)"
    )
    assert _any_line_contains(log, 'sys_platform == "never"'), (
        "the opencv-python override must use a never-matching marker to drop it"
    )


def test_local_wheel_override_installs_wheel_not_pypi(
    env: dict[str, str], tmp_path: Path
) -> None:
    """QUARRY_LOCAL_WHEEL installs the given wheel instead of the PyPI pin.

    This is the hook the clean-machine harness uses to exercise a working-tree
    wheel; without it install.sh could only ever validate the released version.
    """
    wheel = tmp_path / "punt_quarry-9.9.9-py3-none-any.whl"
    wheel.write_bytes(b"PK\x03\x04 not a real wheel, only non-empty for the check")
    local_env = {**env, "QUARRY_LOCAL_WHEEL": str(wheel)}

    result = _run_script(INSTALL_SH, local_env)
    assert result.returncode == 0, (
        f"install.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    log = _read_log(local_env)
    # The wheel path is the uv tool install target ...
    assert _any_line_contains(log, str(wheel)), (
        "the local wheel path must be the uv tool install target"
    )
    # ... and the PyPI pin is NOT used as a fallback.
    assert not _any_line_contains(log, "punt-quarry=="), (
        "must not fall back to the PyPI pin when QUARRY_LOCAL_WHEEL is set"
    )


def test_local_wheel_missing_file_fails_clearly(env: dict[str, str]) -> None:
    """A QUARRY_LOCAL_WHEEL pointing at a missing file fails with a clear message,
    not an obscure uv error."""
    local_env = {**env, "QUARRY_LOCAL_WHEEL": "/nonexistent/quarry.whl"}
    result = _run_script(INSTALL_SH, local_env)
    assert result.returncode != 0, "a missing local wheel must fail the install"
    combined = result.stdout + result.stderr
    assert "QUARRY_LOCAL_WHEEL" in combined, (
        "the failure must name QUARRY_LOCAL_WHEEL so the cause is obvious"
    )


def test_overrides_tempfile_cleaned_up_on_failure(
    env: dict[str, str], tmp_path: Path
) -> None:
    """The uv overrides temp file is removed even when install.sh aborts between
    the mktemp and the install (Class-1 cleanup via an EXIT/INT/TERM trap).

    A missing QUARRY_LOCAL_WHEEL trips the validation branch, which no longer has
    a manual ``rm`` — only the trap can clean the temp file. With TMPDIR pointed
    at a fresh dir, any survivor is a leak.
    """
    tmpdir = tmp_path / "ovr-tmp"
    tmpdir.mkdir()
    local_env = {
        **env,
        "TMPDIR": str(tmpdir),
        "QUARRY_LOCAL_WHEEL": "/nonexistent/quarry.whl",
    }
    result = _run_script(INSTALL_SH, local_env)
    assert result.returncode != 0, "a missing local wheel must fail the install"
    leftovers = list(tmpdir.iterdir())
    assert leftovers == [], f"overrides temp file leaked on abort: {leftovers}"


def test_overrides_tempfile_cleaned_up_on_success(
    env: dict[str, str], tmp_path: Path
) -> None:
    """On a normal install the overrides temp file is removed (rm + trap release)
    and nothing is left behind in TMPDIR."""
    tmpdir = tmp_path / "ovr-tmp-ok"
    tmpdir.mkdir()
    local_env = {**env, "TMPDIR": str(tmpdir)}
    result = _run_script(INSTALL_SH, local_env)
    assert result.returncode == 0, (
        f"install.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    leftovers = list(tmpdir.iterdir())
    assert leftovers == [], f"overrides temp file leaked on success: {leftovers}"
