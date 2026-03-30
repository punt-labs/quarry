"""Remote quarry server configuration via mcp-proxy config file."""

from __future__ import annotations

import os
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

MCP_PROXY_CONFIG_PATH: Path = Path.home() / ".punt-labs" / "mcp-proxy" / "quarry.toml"


class PermissionWarning(Warning):
    """Raised when config was written but file permissions could not be restricted."""


def _toml_escape(value: str) -> str:
    """Escape a string value for use inside a TOML basic string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def read_proxy_config() -> dict[str, Any]:
    """Return parsed mcp-proxy config, or {} if file does not exist."""
    if not MCP_PROXY_CONFIG_PATH.exists():
        return {}
    try:
        return tomllib.loads(MCP_PROXY_CONFIG_PATH.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"Malformed config at {MCP_PROXY_CONFIG_PATH}: {exc}. "
            "Delete the file and run 'quarry login' again."
        ) from exc


def write_proxy_config(url: str, token: str) -> None:
    """Write quarry section to mcp-proxy config file atomically, chmod 0600."""
    content = (
        "[quarry]\n"
        f'url = "{_toml_escape(url)}"\n'
        "\n"
        "[quarry.headers]\n"
        f'Authorization = "Bearer {_toml_escape(token)}"\n'
    )
    MCP_PROXY_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MCP_PROXY_CONFIG_PATH.with_suffix(".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp), flags, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
    except:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(MCP_PROXY_CONFIG_PATH)
    try:
        MCP_PROXY_CONFIG_PATH.chmod(0o600)  # belt-and-suspenders for overwrite case
    except OSError as exc:
        raise PermissionWarning(
            f"Config written to {MCP_PROXY_CONFIG_PATH} but could not restrict "
            f"permissions: {exc}. The token is stored but may be readable by other "
            "users on this system."
        ) from exc


def delete_proxy_config() -> bool:
    """Remove [quarry] section from mcp-proxy config.

    Return False if nothing to remove.
    """
    if not MCP_PROXY_CONFIG_PATH.exists():
        return False
    raw = MCP_PROXY_CONFIG_PATH.read_text()
    # Strip the [quarry] block including all [quarry.*] subsections.
    # Stop at the next section that is not a quarry subsection (any [header]
    # except [quarry] and [quarry.*]) or EOF.
    cleaned, n_subs = re.subn(
        r"\[quarry\].*?(?=\n\[(?!quarry)[^\]]*\]|\Z)", "", raw, flags=re.DOTALL
    )
    if n_subs == 0:
        return False
    stripped = cleaned.strip()
    if stripped:
        MCP_PROXY_CONFIG_PATH.write_text(stripped + "\n")
        MCP_PROXY_CONFIG_PATH.chmod(0o600)
    else:
        MCP_PROXY_CONFIG_PATH.unlink()
    return True


def _ws_to_http(url: str) -> str:
    """Convert ws:// or wss:// URL to http:// or https:// for validation."""
    if url.startswith("wss://"):
        return "https://" + url[6:]
    if url.startswith("ws://"):
        return "http://" + url[5:]
    return url


def validate_connection(
    host: str, port: int, token: str, scheme: str = "http"
) -> tuple[bool, str]:
    """HTTP(S) GET /status with Bearer token. Return (ok, error_message)."""
    url = f"{scheme}://{host}:{port}/status"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=10) as _:  # noqa: S310
            return True, ""
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "Authentication failed — check --api-key."
        return False, f"Server returned {exc.code}."
    except (urllib.error.URLError, OSError) as exc:
        reason: object = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        return False, f"Could not connect to {host}:{port} — {reason}."


def validate_connection_from_ws_url(ws_url: str, token: str) -> tuple[bool, str]:
    """Parse ws:// or wss:// URL and validate via HTTP/HTTPS."""
    http_url = _ws_to_http(ws_url)
    parsed = urllib.parse.urlparse(http_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8420
    scheme = parsed.scheme or "http"
    return validate_connection(host, port, token, scheme=scheme)


def mask_token(token: str) -> str:
    """Return first 4 chars + **** for display."""
    if len(token) < 4:
        return "****"
    return token[:4] + "****"
