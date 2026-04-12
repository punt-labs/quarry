# ruff: noqa: S603, S607 — all subprocess calls invoke system binaries (launchctl, systemctl, loginctl)
"""Daemon lifecycle management for ``quarry serve``.

Provides ``install`` and ``uninstall`` commands that register quarry as a
system service (launchd on macOS, systemd on Linux) so the daemon starts
at login and restarts on crash.

The service runs ``quarry serve --port 8420`` using the installed ``quarry``
binary (from ``uv tool install``), never ``sys.executable``.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from quarry.config import DEFAULT_PORT
from quarry.tls import TLS_DIR, cert_fingerprint, write_tls_files

logger = logging.getLogger(__name__)

_LABEL = "com.punt-labs.quarry"
_ENV_FILE: Path = Path.home() / ".punt-labs" / "quarry" / "quarry.env"


def _write_env_file(api_key: str) -> None:
    """Write QUARRY_API_KEY to the env file atomically with mode 0600.

    Uses os.open() so the file is created with restrictive permissions
    from the start — no chmod race window.  Writes to a .tmp sibling then
    renames into place.

    Args:
        api_key: The API key value to store.
    """
    _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _ENV_FILE.with_name(_ENV_FILE.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp_path), flags, 0o600)
    try:
        f = os.fdopen(fd, "w")
    except BaseException:
        os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise
    try:
        with f:
            escaped = api_key.replace("\\", "\\\\").replace('"', '\\"')
            f.write(f'QUARRY_API_KEY="{escaped}"\n')
        tmp_path.replace(_ENV_FILE)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _quarry_exec_args() -> list[str]:
    """Return the command to invoke ``quarry serve``.

    Resolution order:

    1. ``~/.local/bin/quarry`` (uv tool install symlink) -- resolved to absolute.
    2. Refuse to register -- raise ``RuntimeError`` instead of silently using
       ``sys.executable`` or ``shutil.which()``, either of which may resolve
       to a dev venv binary.

    The ``sys.executable`` fallback is deliberately removed.  When ``quarry
    install`` runs from a dev venv, ``sys.executable`` is ``.venv/bin/python3``
    which has CPU-only onnxruntime and no GPU provider.  The systemd/launchd
    unit bakes this path permanently, and the daemon crash-loops until someone
    manually edits the unit or re-runs the installer.

    Reads ``QUARRY_SERVE_HOST`` from the environment at registration time.
    When set and non-empty, ``--host <value>`` is baked into the service command
    so the daemon binds to the correct address after reboot.  If unset, the
    server defaults to loopback (``127.0.0.1``).

    The API key is NOT included in exec args -- it is passed to the daemon via
    an env file (``~/.punt-labs/quarry/quarry.env``) to keep it out of
    ``ps aux`` output and world-readable service files.

    Appends ``--tls`` when TLS certificates are present in TLS_DIR.
    """
    # 1. Preferred: uv tool install symlink.
    local_bin = Path.home() / ".local" / "bin" / "quarry"
    if local_bin.exists():
        resolved = local_bin.resolve()
        logger.info("Service binary: %s (uv tool)", resolved)
        base = [str(resolved), "serve", "--port", str(DEFAULT_PORT)]
    else:
        msg = (
            "Cannot find quarry binary at ~/.local/bin/quarry. "
            "Install quarry first: uv tool install punt-quarry"
        )
        raise RuntimeError(msg)

    serve_host = os.environ.get("QUARRY_SERVE_HOST", "").strip()
    if serve_host:
        base.extend(["--host", serve_host])

    cert_path = TLS_DIR / "server.crt"
    key_path = TLS_DIR / "server.key"
    if cert_path.exists() and key_path.exists():
        base.append("--tls")
    elif cert_path.exists() or key_path.exists():
        logger.warning(
            "Partial TLS state in %s — only one of server.crt / server.key exists. "
            "Run 'quarry install' to regenerate TLS files.",
            TLS_DIR,
        )

    return base


# ---------------------------------------------------------------------------
# macOS — launchd
# ---------------------------------------------------------------------------

_LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
_LAUNCHD_PLIST = _LAUNCHD_DIR / f"{_LABEL}.plist"


def _launchd_plist_content() -> str:
    args = _quarry_exec_args()
    program_args = "\n".join(f"        <string>{_xml_escape(a)}</string>" for a in args)
    log_dir = Path.home() / ".punt-labs" / "quarry" / "logs"
    # launchd does not support EnvironmentFile — embed the API key directly in the
    # plist EnvironmentVariables dict.  The plist is written at install time (0700
    # LaunchAgents dir, 0600 plist) by the installing user, so this matches the
    # security posture of any other credential in a launchd plist.
    api_key = os.environ.get("QUARRY_API_KEY", "").strip()
    env_vars_block = ""
    if api_key:
        escaped_key = _xml_escape(api_key)
        # Do NOT use textwrap.dedent here — the outer dedent computes minimum
        # indent across ALL lines including the substituted env_vars_block.
        # A nested dedent produces 0-space lines, defeating the outer dedent.
        env_vars_block = (
            "<key>EnvironmentVariables</key>\n"
            "        <dict>\n"
            "            <key>QUARRY_API_KEY</key>\n"
            f"            <string>{escaped_key}</string>\n"
            "        </dict>\n"
            "        "
        )
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
          "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>{_LABEL}</string>
            <key>ProgramArguments</key>
            <array>
        {program_args}
            </array>
            {env_vars_block}<key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>StandardOutPath</key>
            <string>{log_dir}/quarry-stdout.log</string>
            <key>StandardErrorPath</key>
            <string>{log_dir}/quarry-stderr.log</string>
        </dict>
        </plist>
    """)


def _launchd_install() -> None:
    _LAUNCHD_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)

    # Unload any existing service first — handles upgrades where the
    # old plist pointed to a different binary (e.g. editable install).
    # Without this, `launchctl load` fails silently or with I/O error
    # when the label is already registered, and the old binary keeps
    # respawning via KeepAlive.
    if _launchd_status():
        result = subprocess.run(
            ["launchctl", "unload", "-w", str(_LAUNCHD_PLIST)],
            check=False,
        )
        if result.returncode == 0:
            logger.info("Unloaded existing %s before upgrade", _LABEL)
        else:
            logger.warning(
                "Could not unload %s (rc=%d) — proceeding with load",
                _LABEL,
                result.returncode,
            )

    content = _launchd_plist_content()
    tmp_path = _LAUNCHD_PLIST.with_name(_LAUNCHD_PLIST.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp_path), flags, 0o600)
    try:
        f = os.fdopen(fd, "w")
    except BaseException:
        os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise
    try:
        with f:
            f.write(content)
        tmp_path.replace(_LAUNCHD_PLIST)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    logger.info("Wrote %s", _LAUNCHD_PLIST)

    subprocess.run(
        ["launchctl", "load", "-w", str(_LAUNCHD_PLIST)],
        check=True,
    )
    logger.info("Loaded %s into launchd", _LABEL)


def _launchd_uninstall() -> None:
    if _LAUNCHD_PLIST.exists():
        subprocess.run(
            ["launchctl", "unload", "-w", str(_LAUNCHD_PLIST)],
            check=False,  # may already be unloaded
        )
        _LAUNCHD_PLIST.unlink()
        logger.info("Removed %s", _LAUNCHD_PLIST)
    else:
        logger.info("No plist found at %s — nothing to uninstall", _LAUNCHD_PLIST)


def _launchd_status() -> bool:
    result = subprocess.run(
        ["launchctl", "list", _LABEL],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Linux — systemd user unit
# ---------------------------------------------------------------------------

_SYSTEMD_DIR = Path.home() / ".config" / "systemd" / "user"
_SYSTEMD_UNIT = _SYSTEMD_DIR / "quarry.service"


def _systemd_escape(arg: str) -> str:
    """Escape a single argument for use in systemd unit ExecStart.

    systemd uses its own parser, not POSIX shell.  Double-quote the value
    and backslash-escape embedded double-quotes and backslashes.
    Single-quote POSIX shell escaping (e.g. ``'foo'"'"'bar'``) is invalid
    in systemd ExecStart and must not be used.
    """
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _systemd_unit_content() -> str:
    args = _quarry_exec_args()
    exec_start = " ".join(_systemd_escape(a) for a in args)
    env_file_path = str(_ENV_FILE)
    return textwrap.dedent(f"""\
        [Unit]
        Description=Quarry semantic search daemon
        After=network.target

        [Service]
        ExecStart={exec_start}
        EnvironmentFile=-{env_file_path}
        Restart=on-failure
        RestartSec=5

        [Install]
        WantedBy=default.target
    """)


def _systemd_install() -> None:
    _SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    _SYSTEMD_UNIT.write_text(_systemd_unit_content())
    logger.info("Wrote %s", _SYSTEMD_UNIT)

    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        check=True,
    )
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", "quarry"],
        check=True,
    )
    # Force restart to pick up new unit file and TLS certs.
    # enable --now starts a stopped service but does not restart a running one.
    subprocess.run(
        ["systemctl", "--user", "restart", "quarry"],
        check=True,
    )
    logger.info("Enabled and restarted quarry.service")


def _systemd_uninstall() -> None:
    if _SYSTEMD_UNIT.exists():
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "quarry"],
            check=False,  # may already be stopped
        )
        _SYSTEMD_UNIT.unlink()
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            check=False,
        )
        logger.info("Removed %s", _SYSTEMD_UNIT)
    else:
        logger.info("No unit found at %s — nothing to uninstall", _SYSTEMD_UNIT)


def _systemd_status() -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "quarry"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == "active"


# ---------------------------------------------------------------------------
# GPU runtime
# ---------------------------------------------------------------------------


def ensure_gpu_runtime() -> str:
    """Swap onnxruntime for onnxruntime-gpu when an NVIDIA GPU is present.

    Safe to call on any platform -- returns early when nvidia-smi is absent
    (macOS, CPU-only Linux).  Uses ``uv pip`` to swap the package inside
    the current interpreter's environment.

    Returns a status string for display:
      - ``"no NVIDIA GPU"``
      - ``"CUDA already available"``
      - ``"onnxruntime-gpu installed"``
      - ``"onnxruntime-gpu install failed, CPU restored"``
      - ``"onnxruntime-gpu install failed, CPU restore also failed"``
      - ``"uv not found, skipped GPU check"``
    """
    uv_path = shutil.which("uv")
    if uv_path is None:
        logger.info("uv not on PATH — skipping GPU runtime check")
        return "uv not found, skipped GPU check"

    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        logger.info("nvidia-smi not found — no NVIDIA GPU")
        return "no NVIDIA GPU"

    result = subprocess.run(
        [nvidia_smi],
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        logger.info(
            "nvidia-smi failed (rc=%d) — no usable NVIDIA GPU",
            result.returncode,
        )
        return "no NVIDIA GPU"

    # GPU is present — check if CUDA provider is already available.
    # Use a subprocess to avoid stale native shared libraries (.so) that
    # persist in the current process after a previous onnxruntime import.
    provider_check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import onnxruntime; "
            "print(','.join(onnxruntime.get_available_providers()))",
        ],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    cuda_available = (
        provider_check.returncode == 0
        and "CUDAExecutionProvider" in provider_check.stdout
    )
    if cuda_available:
        logger.info("CUDAExecutionProvider already available")
        return "CUDA already available"

    python = sys.executable
    logger.info("Swapping onnxruntime for onnxruntime-gpu (python=%s)", python)

    # Uninstall CPU onnxruntime (suppress errors — may not be installed).
    subprocess.run(
        [uv_path, "pip", "uninstall", "--python", python, "onnxruntime"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )

    # Install onnxruntime-gpu.
    gpu_install = subprocess.run(
        [uv_path, "pip", "install", "--python", python, "onnxruntime-gpu>=1.18.0"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    if gpu_install.returncode == 0:
        logger.info("onnxruntime-gpu installed successfully")
        # Clear stale module cache so subsequent imports see the new package.
        sys.modules.pop("onnxruntime", None)
        return "onnxruntime-gpu installed"

    # GPU install failed — restore CPU onnxruntime.
    logger.warning(
        "onnxruntime-gpu install failed (rc=%d), restoring CPU runtime",
        gpu_install.returncode,
    )
    cpu_restore = subprocess.run(
        [uv_path, "pip", "install", "--python", python, "onnxruntime>=1.18.0"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    # Clear stale module cache so subsequent imports see the restored package.
    sys.modules.pop("onnxruntime", None)
    if cpu_restore.returncode != 0:
        logger.error(
            "CPU onnxruntime restore also failed (rc=%d)",
            cpu_restore.returncode,
        )
        return "onnxruntime-gpu install failed, CPU restore also failed"
    return "onnxruntime-gpu install failed, CPU restored"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _get_tls_hostname() -> str:
    """Return the best available hostname for TLS certificate SANs.

    Preference order:
    1. ``QUARRY_TLS_HOSTNAME`` env var — explicit override for production FQDNs.
    2. ``socket.getfqdn()`` when it contains a dot (looks like a real FQDN).
    3. ``socket.gethostname()`` fallback.
    """
    env_hostname = os.environ.get("QUARRY_TLS_HOSTNAME", "").strip()
    if env_hostname:
        return env_hostname
    fqdn = socket.getfqdn()
    if fqdn and "." in fqdn:
        return fqdn
    return socket.gethostname()


def detect_platform() -> str:
    """Return ``'macos'`` or ``'linux'``.  Raises on unsupported platforms."""
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Linux":
        return "linux"
    msg = f"Unsupported platform: {system}. quarry install supports macOS and Linux."
    raise SystemExit(msg)


def install() -> str:
    """Install quarry as a system service.  Returns a status message."""
    # Validate that non-loopback binds have an API key — without one the daemon
    # will crash-loop at runtime because http_server.serve() enforces this invariant.
    serve_host = os.environ.get("QUARRY_SERVE_HOST", "").strip()
    api_key = os.environ.get("QUARRY_API_KEY", "").strip()
    if serve_host and serve_host != "127.0.0.1" and not api_key:
        msg = (
            f"QUARRY_SERVE_HOST is set to {serve_host!r} but QUARRY_API_KEY is empty. "
            "Non-loopback hosts require an API key. "
            "Set QUARRY_API_KEY before running 'quarry install'."
        )
        raise SystemExit(msg)

    plat = detect_platform()

    # Linux: API key written to ~/.punt-labs/quarry/quarry.env (0600) before
    # service registration so systemd can read it via EnvironmentFile= on first
    # start.  The key is NOT baked into ExecStart args to stay out of ps output.
    # macOS: API key embedded in the plist EnvironmentVariables block only —
    # no env file is written.
    if api_key and plat == "linux":
        _write_env_file(api_key)

    # Generate TLS certificates before registering the service so that the
    # service file can include --tls in its exec args.
    hostname = _get_tls_hostname()
    write_tls_files(hostname)
    ca_crt = TLS_DIR / "ca.crt"
    fingerprint = cert_fingerprint(ca_crt.read_bytes()) if ca_crt.exists() else ""

    args = _quarry_exec_args()

    if plat == "macos":
        _launchd_install()
        running = _launchd_status()
    else:
        _systemd_install()
        running = _systemd_status()

    exec_display = " ".join(args)
    status = "running" if running else "installed (not yet running)"
    lines = [
        f"quarry daemon {status} on port {DEFAULT_PORT}.",
        f"  Service: {_LAUNCHD_PLIST if plat == 'macos' else _SYSTEMD_UNIT}",
        f"  Command: {exec_display}",
    ]
    if fingerprint:
        lines.append(f"  CA fingerprint: {fingerprint}")
        lines.append(
            "  Clients: run 'quarry login <this-host> --api-key <token>' "
            "to connect and trust this fingerprint."
        )
    if plat == "linux" and not _has_linger():
        lines.append(
            "  Warning: loginctl linger is not enabled. "
            "The daemon will stop when you log out. "
            "Run: loginctl enable-linger"
        )
    return os.linesep.join(lines)


def uninstall() -> str:
    """Remove quarry system service.  Returns a status message."""
    plat = detect_platform()
    if plat == "macos":
        _launchd_uninstall()
        path = _LAUNCHD_PLIST
    else:
        _systemd_uninstall()
        path = _SYSTEMD_UNIT
    return f"quarry daemon uninstalled. Removed {path}."


def _has_linger() -> bool:
    """Check if loginctl linger is enabled for the current user (Linux only)."""
    result = subprocess.run(
        ["loginctl", "show-user", os.getlogin(), "--property=Linger"],
        capture_output=True,
        text=True,
    )
    return "Linger=yes" in result.stdout
