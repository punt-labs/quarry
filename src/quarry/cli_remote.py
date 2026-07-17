"""The ``login`` / ``logout`` commands and the ``remote`` inspection sub-app.

``login`` performs the TOFU certificate-pinning handshake (fetch CA, confirm
fingerprint, validate over TLS, persist) and stays here rather than in the
transport tier because it is an interactive trust-establishment flow.  Its steps
are split into small methods so no single function exceeds the complexity budget;
the security-sensitive tempfile and rollback handling is preserved verbatim.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Self, final

import typer

from quarry.client import ClientConfig, QuarryConnectionError, TargetResolver
from quarry.config import DEFAULT_PORT
from quarry.remote import (
    CA_CERT_PATH,
    MCP_PROXY_CONFIG_PATH,
    PermissionWarning,
    delete_proxy_config,
    fetch_ca_cert,
    mask_token,
    store_ca_cert,
    to_netloc,
    validate_connection,
    write_proxy_config,
)
from quarry.tls import cert_fingerprint

if TYPE_CHECKING:
    from quarry.cli_captures import CliPlumbing


@final
@dataclass(frozen=True, slots=True)
class LoginTarget:
    """The immutable inputs of one login attempt (no I/O — pure derivations)."""

    _host: str
    _port: int
    _api_key: str | None
    _is_loopback: bool

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def api_key(self) -> str | None:
        # None = an unauthenticated remote server (no --api-key supplied).
        return self._api_key

    @property
    def is_loopback(self) -> bool:
        return self._is_loopback

    @property
    def stored_key(self) -> str | None:
        # A loopback bearer is the LIVE serve.token, resolved fresh each call —
        # never persisted; only a remote login stores its operator key.
        return None if self._is_loopback else self._api_key

    @property
    def ws_url(self) -> str:
        """The ``wss://`` URL stored for this target (IPv6 host bracketed)."""
        return f"wss://{to_netloc(self._host, self._port)}/mcp"

    @classmethod
    def create(cls, host: str, port: int, api_key: str | None) -> Self:
        """Build a target, classifying the (already-canonical) host as loopback."""
        return cls(host, port, api_key, ClientConfig.is_loopback_host(host))


@final
class RemoteCli:
    """Serve ``login``/``logout`` and the ``remote`` sub-app on injected plumbing."""

    __slots__ = ("_p",)

    _p: CliPlumbing

    def __new__(cls, plumbing: CliPlumbing) -> Self:
        self = super().__new__(cls)
        self._p = plumbing
        return self

    def register(self, app: typer.Typer) -> None:
        """Attach ``login``, ``logout``, and the ``remote`` sub-app to *app*."""
        app.command(name="login")(self._p.cli_errors(self._login))
        app.command(name="logout")(self._p.cli_errors(self._logout))
        app.add_typer(self._build_remote(), name="remote")

    def _login(
        self,
        host: Annotated[str, typer.Argument(help="Remote quarry host or IP")],
        port: Annotated[int, typer.Option("--port", "-p", help="Port")] = DEFAULT_PORT,
        api_key: Annotated[
            str | None,
            typer.Option(
                "--api-key",
                help="Bearer token for remote server (omit for unauthenticated)",
                hide_input=True,
                envvar="QUARRY_API_KEY",
            ),
        ] = None,
        yes: Annotated[
            bool,
            typer.Option("--yes", "-y", help="Skip the TOFU confirmation prompt."),
        ] = False,
    ) -> None:
        """Connect to a remote quarry server using TOFU certificate pinning.

        Fetches the server's CA over HTTPS (TOFU bootstrap), displays its
        fingerprint, prompts for trust, validates over HTTPS/WSS, and writes the
        pinned CA + login config.
        """
        # Canonicalize a loopback NAME to the literal the daemon binds, and strip
        # the operator key exactly as the daemon does (a trailing newline would
        # otherwise be presented verbatim and 401'd).
        target = LoginTarget.create(
            ClientConfig.canonical_host(host), port, (api_key or "").strip() or None
        )
        ca_pem = self._trust(target, yes=yes)
        self._validate(target, ca_pem)
        self._persist(target, ca_pem)
        self._p.emit(
            {"host": target.host, "port": target.port},
            f"Logged in to {target.host}:{target.port}. Restart Claude Code to apply.",
        )

    def _trust(self, target: LoginTarget, *, yes: bool) -> bytes:
        """Fetch the CA, show its fingerprint, and confirm trust (TOFU bootstrap)."""
        try:
            ca_pem = fetch_ca_cert(target.host, target.port)
        except ValueError as exc:
            self._p.err_console.print(f"Error: {exc}", style="red")
            raise typer.Exit(code=1) from exc
        if not self._p.is_quiet():
            self._p.err_console.print(
                f"Server CA fingerprint: {cert_fingerprint(ca_pem)}"
            )
        if not yes and not typer.confirm("Trust this server?", default=False):
            if not self._p.is_quiet():
                self._p.err_console.print("Aborted. Not logged in.")
            raise typer.Exit(code=0)
        return ca_pem

    def _validate(self, target: LoginTarget, ca_pem: bytes) -> None:
        """Validate the connection using the CA in a tempfile (removed on exit).

        A loopback target authenticates with the daemon's LIVE serve.token, not
        ``--api-key``; a remote target uses the operator key.
        """
        bearer = (
            ClientConfig.loopback_token(target.host)
            if target.is_loopback
            else target.api_key
        )
        tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".crt")
        tmp_path = Path(tmp_path_str)
        try:
            try:
                tmp_file = os.fdopen(tmp_fd, "wb")
            except BaseException:
                os.close(tmp_fd)
                tmp_path.unlink(missing_ok=True)
                raise
            try:
                with tmp_file:
                    tmp_file.write(ca_pem)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
            ok, reason = validate_connection(
                target.host,
                target.port,
                bearer,
                scheme="https",
                ca_cert_path=tmp_path_str,
            )
            if not ok:
                self._p.err_console.print(f"Error: {reason}", style="red")
                raise typer.Exit(code=1)
        finally:
            tmp_path.unlink(missing_ok=True)

    def _persist(self, target: LoginTarget, ca_pem: bytes) -> None:
        """Write the login config first, then the pinned CA, rolling back on failure.

        Config-before-cert so a CA-write failure can roll the config back; the
        reverse order has no recovery path.
        """
        try:
            write_proxy_config(target.ws_url, target.stored_key, str(CA_CERT_PATH))
        except PermissionWarning as exc:
            self._p.err_console.print(f"Warning: {exc}", style="yellow")
        except OSError as exc:
            self._p.err_console.print(
                f"Error: connection succeeded but could not write config to "
                f"{MCP_PROXY_CONFIG_PATH}: {exc}",
                style="red",
            )
            raise typer.Exit(code=1) from exc
        try:
            store_ca_cert(ca_pem)
        except Exception as exc:
            with contextlib.suppress(OSError):
                delete_proxy_config()
            self._p.err_console.print(
                f"Error: could not store CA certificate: {exc}", style="red"
            )
            raise typer.Exit(code=1) from exc

    def _logout(self) -> None:
        """Disconnect from the remote server and revert to the local daemon."""
        if delete_proxy_config():
            self._p.emit(
                {"logged_out": True},
                "Logged out. Restart Claude Code to revert to local daemon.",
            )
        else:
            self._p.emit({"logged_out": False}, "No remote configured.")

    def _build_remote(self) -> typer.Typer:
        """Build the ``remote`` sub-app (a single ``list`` command)."""
        app = typer.Typer(
            help="Manage remote quarry server connection.",
            invoke_without_command=True,
            rich_markup_mode=None,
        )
        app.callback(invoke_without_command=True)(self._remote_callback)
        app.command(name="list")(self._p.cli_errors(self._remote_list))
        return app

    def _remote_callback(self, ctx: typer.Context) -> None:
        """Manage remote quarry server connection."""
        if ctx.invoked_subcommand is None:
            self._p.err_console.print(
                "Error: specify a subcommand — list.", style="red"
            )
            raise typer.Exit(code=1)

    def _remote_list(
        self,
        ping: Annotated[
            bool, typer.Option("--ping", help="Check daemon health via /health")
        ] = False,
    ) -> None:
        """Show the daemon target commands actually resolve to; with --ping, its health.

        Reports the target :class:`TargetResolver` selects — explicit
        ``QUARRY_URL``, then a stored login, then the loopback daemon — so this
        never diverges from where data commands really go.
        """
        try:
            cfg = TargetResolver.resolve()
        except QuarryConnectionError as exc:
            self._p.emit(
                {"target": None, "reason": exc.message},
                f"No daemon target resolved: {exc.message}",
            )
            return
        token = mask_token(cfg.token) if cfg.token else "(none)"
        data: dict[str, object] = {"url": cfg.url, "token_prefix": token}
        text = f"Target: {cfg.url}  token: {token}"
        if ping:
            health = self._p.client().health()
            data["health"] = health.model_dump()
            text += (
                f"\nHealth: {health.status} (state={health.state}, "
                f"api={health.api_version}, quarry={health.quarry_version})"
            )
        self._p.emit(data, text)
