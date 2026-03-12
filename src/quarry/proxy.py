# ruff: noqa: S310 — URLs constructed from known constants, not user input
"""Download and install the ``mcp-proxy`` binary from GitHub Releases.

The proxy bridges MCP stdio transport to the quarry daemon over WebSocket,
eliminating Python startup cost for every Claude Code session.
"""

from __future__ import annotations

import hashlib
import logging
import platform
import shutil
import stat
import tempfile
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO = "punt-labs/mcp-proxy"
_INSTALL_DIR = Path.home() / ".local" / "bin"
_BINARY_NAME = "mcp-proxy"
_UA = "punt-quarry (https://github.com/punt-labs/quarry)"


def _request(url: str, **headers: str) -> urllib.request.Request:
    """Build a request with User-Agent always set."""
    h = {"User-Agent": _UA, **headers}
    return urllib.request.Request(url, headers=h)


def _asset_name() -> str:
    """Return the platform-specific asset name for the current machine."""
    system = platform.system().lower()  # darwin, linux
    machine = platform.machine().lower()  # arm64, x86_64, aarch64

    if system not in ("darwin", "linux"):
        msg = f"Unsupported platform: {system}"
        raise SystemExit(msg)

    arch_map = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "amd64",
        "amd64": "amd64",
    }
    arch = arch_map.get(machine)
    if arch is None:
        msg = f"Unsupported architecture: {machine}"
        raise SystemExit(msg)

    return f"mcp-proxy-{system}-{arch}"


def _latest_version() -> str:
    """Fetch the latest release tag from GitHub."""
    url = f"https://api.github.com/repos/{_REPO}/releases/latest"
    req = _request(url, Accept="application/vnd.github+json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        import json  # noqa: PLC0415

        data = json.loads(resp.read())
    return str(data["tag_name"])


def _download_url(version: str, asset: str) -> str:
    """Construct the download URL for a release asset."""
    return (
        f"https://github.com/{_REPO}/releases"
        f"/download/{version}/{asset}"
    )


def _checksums_url(version: str) -> str:
    """Construct the download URL for the checksums file."""
    return (
        f"https://github.com/{_REPO}/releases"
        f"/download/{version}/checksums.txt"
    )


def _verify_checksum(
    binary_path: Path, version: str, asset: str,
) -> None:
    """Verify SHA256 checksum of downloaded binary against release checksums."""
    url = _checksums_url(version)
    req = _request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        checksums_text = resp.read().decode()

    expected = None
    for line in checksums_text.strip().splitlines():
        sha, name = line.split(None, 1)
        if name.strip() == asset:
            expected = sha
            break

    if expected is None:
        binary_path.unlink(missing_ok=True)
        msg = f"No checksum found for {asset} in release {version}"
        raise ValueError(msg)

    actual = hashlib.sha256(binary_path.read_bytes()).hexdigest()
    if actual != expected:
        binary_path.unlink(missing_ok=True)
        msg = (
            f"Checksum mismatch for {asset}: "
            f"expected {expected}, got {actual}"
        )
        raise ValueError(msg)


def installed_path() -> str | None:
    """Return the installed mcp-proxy path if on PATH, else None."""
    path = shutil.which(_BINARY_NAME)
    return path if path else None


def install(*, version: str | None = None) -> str:
    """Download and install mcp-proxy to ~/.local/bin/.

    Downloads to a temporary file, verifies the SHA256 checksum, then
    atomically renames into place. Returns a status message.
    """
    if version is None:
        version = _latest_version()

    asset = _asset_name()
    url = _download_url(version, asset)

    _INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    dest = _INSTALL_DIR / _BINARY_NAME

    # Download to tempfile, verify checksum, then atomic rename
    logger.info("Downloading %s %s", _BINARY_NAME, version)
    req = _request(url)
    fd, tmp_name = tempfile.mkstemp(
        dir=_INSTALL_DIR, suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            import os  # noqa: PLC0415

            os.write(fd, resp.read())
            os.close(fd)

        _verify_checksum(tmp_path, version, asset)

        # Make executable
        tmp_path.chmod(
            tmp_path.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH,
        )

        # Atomic rename into place
        tmp_path.rename(dest)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    logger.info("Installed %s to %s", _BINARY_NAME, dest)

    if shutil.which(_BINARY_NAME) is None:
        return (
            f"{_BINARY_NAME} {version} installed to {dest}\n"
            f"  Warning: {_INSTALL_DIR} is not on PATH"
        )

    return f"{_BINARY_NAME} {version} installed to {dest}"
