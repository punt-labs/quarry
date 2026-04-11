"""Shell-integration tests for install.sh (consolidated installer).

These tests invoke install.sh with different mode flags (no flag, --server,
--client) using a ``PATH`` pointing at mock versions of ``nvidia-smi``,
``uv``, ``curl``, ``ssh``, ``claude``, and ``quarry``.  Each mock records
its invocation to a log file; the tests then read the log and assert the
expected call ordering.

Why a shell test and not a unit test: the bug that motivated this file
(quarry-e4c2) was a drift in the step ordering inside the script itself --
separate install scripts diverged from install.sh and no Python code ever
saw the difference.  Consolidating into a single script with mode flags
eliminates the drift class, but we still need to verify each mode's
conditional logic.

Ordering invariants asserted per CLAUDE.md Class 5:

  (a) ``uv tool install --force`` runs before any ``uv pip`` GPU swap call
  (b) When ``nvidia-smi`` reports an NVIDIA GPU, the GPU swap uninstalls
      ``onnxruntime`` and installs ``onnxruntime-gpu`` *before* ``quarry install``
  (c) When ``nvidia-smi`` is absent, the GPU swap is not invoked at all
  (d) ``quarry install`` runs after the GPU swap (where applicable)
  (e) ``--client`` mode never calls ``quarry install`` -- client machines
      don't download the embedding model or start a local daemon
  (f) ``--server`` mode does not call ``claude`` or plugin commands
  (g) ``--server`` without ``QUARRY_API_KEY`` fails early
  (h) Unknown flags cause the script to exit non-zero
  (i) ``sh -s -- --server`` works (POSIX piped-stdin argument passing)
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

    This simulates ``curl ... | sh -s -- --server`` which is the POSIX way
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
# Default mode (full install)
# ---------------------------------------------------------------------------


def test_default_mode_gpu_swap_runs_before_quarry_install(
    env: dict[str, str], mock_bin: Path
) -> None:
    """Default mode: GPU swap MUST run before ``quarry install``
    when an NVIDIA GPU is present.
    """
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


# ---------------------------------------------------------------------------
# --server mode
# ---------------------------------------------------------------------------


def test_server_mode_gpu_swap_runs_before_quarry_install(
    env: dict[str, str], mock_bin: Path
) -> None:
    """--server mode: the shell-level GPU swap MUST run before
    ``quarry install`` when an NVIDIA GPU is present.
    """
    _write_mock(
        mock_bin / "nvidia-smi",
        'printf "%s" "$(basename "$0")" >> "$LOG_FILE"\n'
        'for a in "$@"; do printf " %s" "$a" >> "$LOG_FILE"; done\n'
        'printf "\\n" >> "$LOG_FILE"\n'
        "exit 0\n",
    )

    result = _run_script(INSTALL_SH, env, args=["--server"])
    assert result.returncode == 0, (
        f"install.sh --server failed:\nstdout:\n{result.stdout}\n"
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


def test_server_mode_skips_gpu_swap_without_nvidia(
    env: dict[str, str], mock_bin: Path
) -> None:
    """--server mode: No nvidia-smi on PATH -> no GPU swap."""
    assert not (mock_bin / "nvidia-smi").exists()

    result = _run_script(INSTALL_SH, env, args=["--server"])
    assert result.returncode == 0, (
        f"install.sh --server failed:\nstdout:\n{result.stdout}\n"
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


def test_server_mode_does_not_invoke_claude(
    env: dict[str, str], mock_bin: Path
) -> None:
    """(f) --server mode must NOT call claude CLI (no plugin, no marketplace)."""
    assert not (mock_bin / "nvidia-smi").exists()

    result = _run_script(INSTALL_SH, env, args=["--server"])
    assert result.returncode == 0, (
        f"install.sh --server failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    log = _read_log(env)
    assert not _any_line_contains(log, "claude"), (
        "--server mode must not invoke claude CLI"
    )


def test_server_mode_fails_without_quarry_api_key(
    env: dict[str, str],
) -> None:
    """(g) --server without QUARRY_API_KEY must fail early."""
    env_no_key = {**env}
    del env_no_key["QUARRY_API_KEY"]

    result = _run_script(INSTALL_SH, env_no_key, args=["--server"])
    assert result.returncode != 0, "--server without QUARRY_API_KEY must exit non-zero"
    assert "QUARRY_API_KEY" in result.stdout, (
        "Error message must mention QUARRY_API_KEY"
    )


# ---------------------------------------------------------------------------
# --client mode
# ---------------------------------------------------------------------------


def test_client_mode_gpu_swap_runs_and_no_quarry_install(
    env: dict[str, str], mock_bin: Path
) -> None:
    """--client mode must run the GPU swap (so ``quarry doctor`` on a
    client reports CUDA providers) but MUST NOT call ``quarry install``
    (clients don't download the 120MB model or start a local daemon).
    """
    _write_mock(
        mock_bin / "nvidia-smi",
        'printf "%s" "$(basename "$0")" >> "$LOG_FILE"\n'
        'for a in "$@"; do printf " %s" "$a" >> "$LOG_FILE"; done\n'
        'printf "\\n" >> "$LOG_FILE"\n'
        "exit 0\n",
    )

    result = _run_script(INSTALL_SH, env, args=["--client"])
    assert result.returncode == 0, (
        f"install.sh --client failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    log = _read_log(env)

    # GPU swap ran.
    tool_install_idx = _index_of(log, "uv tool install --force")
    gpu_install_idx = _index_of(log, "onnxruntime-gpu")
    assert tool_install_idx < gpu_install_idx

    # (e) quarry install is NOT invoked on a client install.
    assert not _any_line_contains(log, "quarry install"), (
        "--client mode must not run 'quarry install' -- clients don't "
        "download the embedding model or start a local daemon"
    )


def test_client_mode_does_not_start_daemon(env: dict[str, str], mock_bin: Path) -> None:
    """--client mode must not call systemctl, launchctl, or health check."""
    assert not (mock_bin / "nvidia-smi").exists()

    result = _run_script(INSTALL_SH, env, args=["--client"])
    assert result.returncode == 0, (
        f"install.sh --client failed:\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )

    log = _read_log(env)
    assert not _any_line_contains(log, "systemctl"), (
        "--client mode must not call systemctl"
    )
    assert not _any_line_contains(log, "launchctl"), (
        "--client mode must not call launchctl"
    )
    # Health check uses curl against localhost:8420 -- client must skip it.
    assert not _any_line_contains(log, "localhost:8420/health"), (
        "--client mode must not health-check the daemon"
    )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_unknown_flag_fails(env: dict[str, str]) -> None:
    """(h) Unknown flags must cause the script to exit non-zero."""
    result = _run_script(INSTALL_SH, env, args=["--bogus"])
    assert result.returncode != 0, "Unknown flag must exit non-zero"
    assert "Unknown option" in result.stderr, (
        "Error message must indicate unknown option"
    )


def test_mutually_exclusive_flags_fail(env: dict[str, str]) -> None:
    """--server and --client are mutually exclusive; both must fail."""
    result = _run_script(INSTALL_SH, env, args=["--server", "--client"])
    assert result.returncode != 0, "--server --client must exit non-zero"
    assert "mutually exclusive" in result.stderr, (
        "Error must mention mutual exclusivity"
    )


def test_help_flag_exits_zero(env: dict[str, str]) -> None:
    """--help must exit 0 and print usage."""
    result = _run_script(INSTALL_SH, env, args=["--help"])
    assert result.returncode == 0
    assert "--server" in result.stdout
    assert "--client" in result.stdout


# ---------------------------------------------------------------------------
# sh -s -- --flag (piped stdin argument passing)
# ---------------------------------------------------------------------------


def test_piped_server_mode_parses_flag(env: dict[str, str], mock_bin: Path) -> None:
    """(i) ``sh -s -- --server`` must correctly parse the --server flag
    when the script is piped via stdin (simulating curl | sh -s -- --server).
    """
    result = _run_script_piped(INSTALL_SH, env, args=["--server"])
    assert result.returncode == 0, (
        f"sh -s -- --server failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    log = _read_log(env)

    # Verify server-mode behavior: quarry install runs, claude does not.
    _index_of(log, "quarry install")
    assert not _any_line_contains(log, "claude"), (
        "piped --server mode must not invoke claude CLI"
    )


def test_piped_client_mode_parses_flag(env: dict[str, str], mock_bin: Path) -> None:
    """``sh -s -- --client`` must correctly parse the --client flag."""
    _write_mock(
        mock_bin / "nvidia-smi",
        'printf "%s" "$(basename "$0")" >> "$LOG_FILE"\n'
        'for a in "$@"; do printf " %s" "$a" >> "$LOG_FILE"; done\n'
        'printf "\\n" >> "$LOG_FILE"\n'
        "exit 0\n",
    )

    result = _run_script_piped(INSTALL_SH, env, args=["--client"])
    assert result.returncode == 0, (
        f"sh -s -- --client failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    log = _read_log(env)
    assert not _any_line_contains(log, "quarry install"), (
        "piped --client mode must not run quarry install"
    )


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
