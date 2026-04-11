"""Shell-integration tests for install.sh, install-server.sh, install-client.sh,
install-both.sh.

These tests invoke the install scripts with a ``PATH`` pointing at mock
versions of ``nvidia-smi``, ``uv``, ``curl``, ``ssh``, ``claude``, and
``quarry``.  Each mock records its invocation to a log file; the tests then
read the log and assert the expected call ordering.

Why a shell test and not a unit test: the bug that motivated this file
(quarry-e4c2) was a drift in the step ordering inside the script itself —
the shell-level GPU swap was deleted from install-server.sh / install-client.sh
when they were split out of install.sh, and no Python code ever saw the
difference.  The only way to catch this class of regression is to exercise
the script end-to-end with mocks.

Ordering invariants asserted per CLAUDE.md Class 5:

  (a) ``uv tool install --force`` runs before any ``uv pip`` GPU swap call
  (b) When ``nvidia-smi`` reports an NVIDIA GPU, the GPU swap uninstalls
      ``onnxruntime`` and installs ``onnxruntime-gpu`` *before* ``quarry install``
  (c) When ``nvidia-smi`` is absent, the GPU swap is not invoked at all
  (d) ``quarry install`` runs after the GPU swap (where applicable)
  (e) ``install-client.sh`` never calls ``quarry install`` — client machines
      don't download the embedding model or start a local daemon
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
INSTALL_SERVER_SH = REPO_ROOT / "install-server.sh"
INSTALL_CLIENT_SH = REPO_ROOT / "install-client.sh"
INSTALL_BOTH_SH = REPO_ROOT / "install-both.sh"


def _write_mock(path: Path, body: str) -> None:
    """Create an executable shell-script mock at ``path``."""
    path.write_text("#!/bin/sh\n" + dedent(body).lstrip())
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def mock_bin(tmp_path: Path) -> Path:
    """Mock ``bin`` directory with stubs for every external command the
    install scripts invoke.

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

    # git — prerequisite check only.
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

    # claude — marketplace/plugin commands.
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

    # uv — subcommands used by the scripts.
    _write_mock(
        bin_dir / "uv",
        log_header + "exit 0\n",
    )

    # quarry — the install scripts do two things with ``quarry``:
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

    # curl — used for health checks in install-server.sh / install-both.sh.
    # Succeed so the health-check loop terminates fast.
    _write_mock(bin_dir / "curl", log_header + "exit 0\n")

    # ssh — install.sh / install-client.sh / install-both.sh test SSH to
    # github.com.  Return a success banner so the HTTPS rewrite is skipped.
    _write_mock(
        bin_dir / "ssh",
        log_header
        + 'printf "Hi there! You successfully authenticated.\\n" >&2\n'
        + "exit 0\n",
    )

    # systemctl / launchctl — used by the belt-and-suspenders restart block.
    _write_mock(bin_dir / "systemctl", log_header + "exit 0\n")
    _write_mock(bin_dir / "launchctl", log_header + "exit 0\n")

    # head / sed / id — real utilities from the host; we add them to the
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


def _run_script(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run ``script`` under /bin/sh with ``env`` and return the result.

    Scripts use ``set -eu`` so any mock stub that exits non-zero will abort
    the run.  Use ``check=False`` because some tests want to inspect the
    exit code.
    """
    return subprocess.run(  # noqa: S603 — /bin/sh with a fixed script path
        ["/bin/sh", str(script)],
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
# install-server.sh
# ---------------------------------------------------------------------------


def test_install_server_gpu_swap_runs_before_quarry_install_when_nvidia_present(
    env: dict[str, str], mock_bin: Path
) -> None:
    """quarry-e4c2: the shell-level GPU swap MUST run before ``quarry install``
    when an NVIDIA GPU is present.  Regression guard — this is the exact
    ordering bug that was deleted from install-server.sh when it was split
    out of install.sh.
    """
    # Present a working nvidia-smi so HAS_NVIDIA=1.
    _write_mock(
        mock_bin / "nvidia-smi",
        'printf "%s" "$(basename "$0")" >> "$LOG_FILE"\n'
        'for a in "$@"; do printf " %s" "$a" >> "$LOG_FILE"; done\n'
        'printf "\\n" >> "$LOG_FILE"\n'
        "exit 0\n",
    )

    result = _run_script(INSTALL_SERVER_SH, env)
    assert result.returncode == 0, (
        f"install-server.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
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


def test_install_server_skips_gpu_swap_without_nvidia(
    env: dict[str, str], mock_bin: Path
) -> None:
    """No nvidia-smi on PATH → no GPU swap.  Prevents the script from
    uninstalling onnxruntime on CPU-only hosts, which would leave quarry
    unable to embed.
    """
    # Deliberately do NOT create a nvidia-smi mock — HAS_NVIDIA stays 0.
    assert not (mock_bin / "nvidia-smi").exists()

    result = _run_script(INSTALL_SERVER_SH, env)
    assert result.returncode == 0, (
        f"install-server.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
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


# ---------------------------------------------------------------------------
# install-client.sh
# ---------------------------------------------------------------------------


def test_install_client_gpu_swap_runs_when_nvidia_present_and_no_quarry_install(
    env: dict[str, str], mock_bin: Path
) -> None:
    """install-client.sh must port the GPU swap (so ``quarry doctor`` on a
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

    result = _run_script(INSTALL_CLIENT_SH, env)
    assert result.returncode == 0, (
        f"install-client.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    log = _read_log(env)

    # GPU swap ran.
    tool_install_idx = _index_of(log, "uv tool install --force")
    gpu_install_idx = _index_of(log, "onnxruntime-gpu")
    assert tool_install_idx < gpu_install_idx

    # (e) quarry install is NOT invoked on a client install.
    assert not _any_line_contains(log, "quarry install"), (
        "install-client.sh must not run 'quarry install' — clients don't "
        "download the embedding model or start a local daemon"
    )


# ---------------------------------------------------------------------------
# install-both.sh
# ---------------------------------------------------------------------------


def test_install_both_gpu_swap_runs_before_quarry_install(
    env: dict[str, str], mock_bin: Path
) -> None:
    """install-both.sh must perform the same GPU swap + quarry install
    ordering as install-server.sh.
    """
    _write_mock(
        mock_bin / "nvidia-smi",
        'printf "%s" "$(basename "$0")" >> "$LOG_FILE"\n'
        'for a in "$@"; do printf " %s" "$a" >> "$LOG_FILE"; done\n'
        'printf "\\n" >> "$LOG_FILE"\n'
        "exit 0\n",
    )

    result = _run_script(INSTALL_BOTH_SH, env)
    assert result.returncode == 0, (
        f"install-both.sh failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    log = _read_log(env)

    tool_install_idx = _index_of(log, "uv tool install --force")
    gpu_install_idx = _index_of(log, "onnxruntime-gpu")
    quarry_install_idx = _index_of(log, "quarry install")

    assert tool_install_idx < gpu_install_idx < quarry_install_idx, (
        f"Expected uv tool install ({tool_install_idx}) < GPU swap ({gpu_install_idx}) "
        f"< quarry install ({quarry_install_idx})"
    )


# ---------------------------------------------------------------------------
# install.sh (reference implementation — guards the source of truth)
# ---------------------------------------------------------------------------


def test_install_sh_gpu_swap_still_runs_before_quarry_install(
    env: dict[str, str], mock_bin: Path
) -> None:
    """install.sh is the reference implementation the split installers port
    from.  If someone accidentally reintroduces the regression here too,
    fail loudly.
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
# Shellcheck — cheap static gate that catches the classes of shell bugs
# ``make check`` would otherwise miss.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [INSTALL_SH, INSTALL_SERVER_SH, INSTALL_CLIENT_SH, INSTALL_BOTH_SH],
    ids=lambda p: p.name,
)
def test_install_script_passes_shellcheck(script: Path) -> None:
    """Per CLAUDE.md Class 5: every install script must pass ``shellcheck -x``."""
    shellcheck_bin = shutil.which("shellcheck")
    if shellcheck_bin is None:
        pytest.fail(
            "shellcheck is required for install-script linting "
            "but was not found on PATH. Install shellcheck in "
            "CI (apt-get install shellcheck) so this gate "
            "cannot be skipped."
        )
    result = subprocess.run(  # noqa: S603 — shellcheck resolved via shutil.which
        [shellcheck_bin, "-x", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"shellcheck failed on {script.name}:\n{result.stdout}\n{result.stderr}"
    )
