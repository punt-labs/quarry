"""The ``quarryd`` launcher: resolve bind options and start the engine daemon.

Only the daemon process imports the engine (DES-031 v2.2 R3); this launcher is
its entry point.  It refuses a remote-reachable bind that carries no operator
key, mints a loopback ``serve.token`` when none is supplied, and hands a
:class:`ServeConfig` to :class:`DaemonServer`.  The bind options are bundled in
one :class:`BindOptions` value object rather than threaded as a long parameter
list, and the CLI surface is a static command so the module stays class-first.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, replace
from typing import Annotated, Self, final

import typer

from quarry.config import DEFAULT_PORT, Settings
from quarry.daemon.server import DaemonServer, ServeConfig
from quarry.net import LoopbackPolicy
from quarry.tls import TLS_DIR

# 256-bit URL-safe token — the loopback bearer minted when no key is supplied.
_TOKEN_BYTES = 32


@final
@dataclass(frozen=True, slots=True)
class BindOptions:
    """The daemon's parsed bind options as one value, not a parameter list."""

    host: str
    port: int
    db: str
    api_key: str | None
    cors_origins: tuple[str, ...]
    tls: bool


@final
class DaemonLauncher:
    """Turn parsed :class:`BindOptions` into a running engine daemon."""

    _options: BindOptions

    def __new__(cls, options: BindOptions) -> Self:
        self = super().__new__(cls)
        self._options = cls._normalized(options)
        return self

    @staticmethod
    def _normalized(options: BindOptions) -> BindOptions:
        """Normalize the bind options once, at the single launcher boundary — the
        actual bind point — so the bind, the key gate, and the client all agree.

        Two normalizations:

        - Strip the api_key and map empty/whitespace -> None so
          ``enforce_bind_key``, ``_effective_key``, and ``DaemonServer`` all see
          the same value.  Without this a whitespace-only ``QUARRY_API_KEY`` is
          truthy at the gate: a loopback bind would fail to mint and then exit at
          the daemon boundary (won't start), and a network bind would pass the
          gate only to fail inconsistently later.  Normalized here, a whitespace
          key is absent everywhere — loopback mints, network is refused AT the
          gate.
        - Canonicalize a loopback-NAME host to the IPv4 literal (localhost ->
          127.0.0.1).  Both a managed service-unit start AND a direct ``quarryd
          --host localhost`` pass through here, so the bind agrees with the
          install probe and ``quarry login``, which use 127.0.0.1.  Binding the
          name would land on ``::1`` on an IPv6-preferring host while the client
          checks 127.0.0.1 (false timeout + 401).  An explicit ``::1`` or a
          non-loopback ``0.0.0.0`` is left as the operator set it; the key gate
          (:meth:`launch`) then runs on the canonical host, so ``localhost`` is
          correctly loopback and needs no operator key.
        """
        api_key = (options.api_key or "").strip() or None
        host = LoopbackPolicy(options.host).canonical_host
        return replace(options, api_key=api_key, host=host)

    def launch(self) -> None:
        """Refuse an unsafe bind, mint the loopback token, and serve."""
        options = self._options
        # Refuse a remote-reachable bind that has only an auto-minted token:
        # that token is unreadable by the remote clients who would need it, so
        # binding there without an operator-set key is false security.  The
        # guard runs against the ORIGINAL key, before the loopback fallback is
        # minted, so an auto-token can never satisfy a network bind.
        LoopbackPolicy(options.host).enforce_bind_key(options.api_key)
        certfile, keyfile = self._tls_paths()
        config = ServeConfig(
            host=options.host,
            port=options.port,
            api_key=self._effective_key(),
            cors_origins=frozenset(options.cors_origins) or None,
            ssl_certfile=certfile,
            ssl_keyfile=keyfile,
        )
        DaemonServer.serve(self._settings(), config)

    def _settings(self) -> Settings:
        name = self._options.db or Settings.read_default_db()
        return Settings.load().resolve_db_paths(name or None)

    def _effective_key(self) -> str:
        """Return the operator's key, or a fresh 256-bit loopback token."""
        return self._options.api_key or secrets.token_urlsafe(_TOKEN_BYTES)

    def _tls_paths(self) -> tuple[str | None, str | None]:
        """Return the (cert, key) paths for a TLS bind, or (None, None).

        Raises ``SystemExit`` if ``--tls`` is set but the certificate material
        is absent, so the daemon fails loud rather than binding plaintext.
        """
        if not self._options.tls:
            return None, None
        cert = TLS_DIR / "server.crt"
        key = TLS_DIR / "server.key"
        if not cert.exists() or not key.exists():
            msg = (
                f"TLS certificate files not found in {TLS_DIR}. "
                "Run 'quarry install' first."
            )
            raise SystemExit(msg)
        return str(cert), str(key)

    @staticmethod
    def cli(
        port: Annotated[
            int,
            typer.Option("--port", "-p", help="Port to bind (0 = OS-assigned)."),
        ] = DEFAULT_PORT,
        host: Annotated[
            str,
            typer.Option("--host", help="Address to bind (127.0.0.1 default)."),
        ] = "127.0.0.1",
        db: Annotated[
            str,
            typer.Option("--db", help="Database name (default: configured default)."),
        ] = "",
        api_key: Annotated[
            str | None,
            typer.Option(
                "--api-key",
                envvar="QUARRY_API_KEY",
                help="Required for non-loopback binds; loopback mints one if unset.",
            ),
        ] = None,
        cors_origin: Annotated[
            list[str] | None,
            typer.Option("--cors-origin", help="Allowed CORS origin (repeatable)."),
        ] = None,
        tls: Annotated[
            bool,
            typer.Option("--tls", help="Serve over TLS (see quarry install)."),
        ] = False,
    ) -> None:
        """Run the Quarry engine daemon (blocks until shutdown)."""
        options = BindOptions(
            host=host,
            port=port,
            db=db,
            api_key=api_key,
            cors_origins=tuple(cors_origin or ()),
            tls=tls,
        )
        DaemonLauncher(options).launch()


def entrypoint() -> None:
    """Console-script target: parse argv and launch the daemon."""
    typer.run(DaemonLauncher.cli)
