"""Shell-integration tests for install.sh (two-mode installer).

These tests invoke install.sh with different flags (no flag, --network)
using a ``PATH`` pointing at mock versions of ``nvidia-smi``, ``uv``,
``curl``, ``ssh``, ``claude``, and ``quarry``.  Each mock records its
invocation to a log file; the tests then read the log and assert the
expected call ordering.

The installer has two modes:

  - **Default** (no flags): full install -- daemon on localhost, TLS,
    plugin (if claude CLI found), local quarry login.
  - **--network**: same as default, but binds daemon to 0.0.0.0 instead
    of localhost.  Requires QUARRY_API_KEY.

Ordering invariants asserted per CLAUDE.md Class 5:

  (a) ``uv tool install --force`` runs before any ``uv pip`` GPU swap call
  (b) When ``nvidia-smi`` reports an NVIDIA GPU, the GPU swap uninstalls
      ``onnxruntime`` and installs ``onnxruntime-gpu`` *before* ``quarry install``
  (c) When ``nvidia-smi`` is absent, the GPU swap is not invoked at all
  (d) ``quarry install`` runs after the GPU swap (where applicable)
  (e) ``--network`` without ``QUARRY_API_KEY`` fails early
  (f) Unknown flags cause the script to exit non-zero
  (g) ``sh -s -- --network`` works (POSIX piped-stdin argument passing)
  (h) Plugin install is skipped (no failure) when claude CLI is absent
  (i) Plugin install runs when claude CLI is present
"""

from __future__ import annotations

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

    # uv -- subcommands used by the script.
    _write_mock(
        bin_dir / "uv",
        log_header + "exit 0\n",
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

    # curl -- used for health checks.  Succeed so the health-check loop
    # terminates fast.
    _write_mock(bin_dir / "curl", log_header + "exit 0\n")

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
    for util in ("head", "sed", "id", "printf", "sleep", "grep", "basename"):
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
    return subprocess.run(  # noqa: S603 -- /bin/sh with a fixed script path
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
    return subprocess.run(  # noqa: S603 -- /bin/sh with controlled stdin
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
    """Default mode runs ``quarry login localhost``."""
    result = _run_script(INSTALL_SH, env)
    assert result.returncode == 0, (
        f"install.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    log = _read_log(env)
    assert _any_line_contains(log, "quarry login"), (
        "Default mode must run quarry login localhost"
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
    assert not _any_line_contains(log, "claude"), (
        "Plugin install must be skipped when claude CLI is absent"
    )

    # quarry install still runs (daemon on localhost).
    _index_of(log, "quarry install")

    # Success message should mention Claude Code not found.
    assert "Claude Code" in result.stdout or "not found" in result.stdout


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
    """(f) Unknown flags must cause the script to exit non-zero."""
    result = _run_script(INSTALL_SH, env, args=["--bogus"])
    assert result.returncode != 0, "Unknown flag must exit non-zero"
    assert "Unknown option" in result.stderr, (
        "Error message must indicate unknown option"
    )


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
    """Removed --server and --client flags must fail as unknown options."""
    for flag in ("--server", "--client"):
        result = _run_script(INSTALL_SH, env, args=[flag])
        assert result.returncode != 0, f"{flag} must exit non-zero"
        assert "Unknown option" in result.stderr, (
            f"{flag} must be reported as unknown option"
        )


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
    result = subprocess.run(  # noqa: S603 -- shellcheck resolved via shutil.which
        [shellcheck_bin, "-x", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"shellcheck failed on install.sh:\n{result.stdout}\n{result.stderr}"
    )
