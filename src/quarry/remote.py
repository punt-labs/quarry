"""Remote quarry server configuration via mcp-proxy config file."""

from __future__ import annotations

import http.client
import os
import re
import ssl
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

MCP_PROXY_CONFIG_PATH: Path = Path.home() / ".punt-labs" / "mcp-proxy" / "quarry.toml"
CA_CERT_PATH: Path = Path.home() / ".punt-labs" / "mcp-proxy" / "quarry-ca.crt"


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


def write_proxy_config(
    url: str,
    token: str | None,
    ca_cert_path: str | None = None,
) -> None:
    """Write quarry section to mcp-proxy config file atomically, chmod 0600.

    Args:
        url: The wss:// or ws:// WebSocket URL for the quarry MCP endpoint.
        token: The Bearer token for authentication, or None for unauthenticated
            servers.  When None the ``[quarry.headers]`` section is omitted.
        ca_cert_path: Optional path to a CA certificate PEM for SSL verification.
            Written as ``ca_cert`` in the TOML when provided.
    """
    lines = [
        "[quarry]\n",
        f'url = "{_toml_escape(url)}"\n',
    ]
    if ca_cert_path is not None:
        lines.append(f'ca_cert = "{_toml_escape(ca_cert_path)}"\n')
    if token is not None:
        lines += [
            "\n",
            "[quarry.headers]\n",
            f'Authorization = "Bearer {_toml_escape(token)}"\n',
        ]
    content = "".join(lines)
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
        tmp = MCP_PROXY_CONFIG_PATH.with_suffix(".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(str(tmp), flags, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(stripped + "\n")
        except:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(MCP_PROXY_CONFIG_PATH)
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
    host: str,
    port: int,
    token: str | None,
    scheme: str = "http",
    ca_cert_path: str | None = None,
) -> tuple[bool, str]:
    """HTTP(S) GET /status with optional Bearer token. Return (ok, error_message).

    Args:
        host: Server hostname or IP.
        port: Server port.
        token: Bearer token for authentication, or None for unauthenticated servers.
        scheme: URL scheme ("http" or "https").
        ca_cert_path: Optional path to a CA certificate PEM.  When provided,
            TLS verification uses this CA instead of the system trust store.
    """
    url = f"{scheme}://{host}:{port}/status"
    auth_headers: dict[str, str] = (
        {"Authorization": f"Bearer {token}"} if token is not None else {}
    )
    req = urllib.request.Request(url, headers=auth_headers)  # noqa: S310
    ssl_ctx: ssl.SSLContext | None = None
    if scheme == "https" and ca_cert_path is not None:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.load_verify_locations(ca_cert_path)
    try:
        with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as _:  # noqa: S310
            return True, ""
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "Authentication failed — check --api-key."
        return False, f"Server returned {exc.code}."
    except (urllib.error.URLError, OSError) as exc:
        reason: object = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        return False, f"Could not connect to {host}:{port} — {reason}."


def validate_connection_from_ws_url(
    ws_url: str,
    token: str | None,
    ca_cert_path: str | None = None,
) -> tuple[bool, str]:
    """Parse ws:// or wss:// URL and validate via HTTP/HTTPS.

    Args:
        ws_url: The ws:// or wss:// WebSocket URL.
        token: Bearer token for authentication, or None.
        ca_cert_path: Optional path to a CA certificate PEM.  When provided,
            TLS verification uses this CA instead of the system trust store.
            Required for servers using self-signed certificates.
    """
    http_url = _ws_to_http(ws_url)
    parsed = urllib.parse.urlparse(http_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8420
    scheme = parsed.scheme or "http"
    return validate_connection(
        host, port, token, scheme=scheme, ca_cert_path=ca_cert_path
    )


def mask_token(token: str) -> str:
    """Return first 4 chars + **** for display."""
    if len(token) < 4:
        return "****"
    return token[:4] + "****"


def fetch_ca_cert(host: str, port: int) -> bytes:
    """Fetch the CA certificate from the quarry server (TOFU bootstrap).

    Connects over HTTPS with SSL verification disabled — no TLS verification is
    possible yet because we don't have the CA cert.  The user verifies the
    fingerprint interactively before trusting.

    This is the TOFU bootstrap step — no TLS verification is possible yet
    because we don't have the CA cert.  The user verifies the fingerprint
    interactively before trusting.  Connecting over HTTPS without verification
    is no less safe than plain HTTP: an attacker who can MITM a TLS endpoint
    can equally MITM plain HTTP.

    Args:
        host: Server hostname or IP.
        port: Server port.

    Returns:
        CA certificate PEM bytes.

    Raises:
        ValueError: If the fetch fails for any reason, with a human-readable
            message describing what went wrong.
    """
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    conn = http.client.HTTPSConnection(host, port, context=ssl_ctx, timeout=10)
    try:
        conn.request("GET", "/ca.crt")
        resp = conn.getresponse()
        if resp.status == 404:
            raise ValueError(
                f"Server at {host}:{port} has no CA certificate. "
                "Run 'quarry install' on the server first."
            )
        if resp.status != 200:
            raise ValueError(
                f"Failed to fetch CA cert from https://{host}:{port}/ca.crt: "
                f"HTTP {resp.status}."
            )
        data: bytes = resp.read()
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(
            f"Could not reach {host}:{port} to fetch CA cert — {exc}. "
            "Check that the quarry server is running and reachable."
        ) from exc
    finally:
        conn.close()

    if not data.strip().startswith(b"-----BEGIN CERTIFICATE-----"):
        raise ValueError(
            f"Server at https://{host}:{port} returned unexpected data for /ca.crt "
            "(expected PEM certificate)."
        )
    return data


def store_ca_cert(cert_pem: bytes) -> None:
    """Write the CA certificate to CA_CERT_PATH atomically, chmod 0600.

    Writes to a .tmp sibling, chmods it, then replaces the destination so a
    crash between write and chmod cannot leave a cert with wrong permissions.

    Args:
        cert_pem: CA certificate in PEM format.
    """
    CA_CERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CA_CERT_PATH.with_suffix(".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(cert_pem)
    except:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(CA_CERT_PATH)
