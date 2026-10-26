"""The ``quarryd`` launcher: resolve bind options and start the engine daemon.

Only the daemon process imports the engine; this launcher is
its entry point.  It refuses a remote-reachable bind that carries no operator
key, mints a loopback ``serve.token`` when none is supplied, and hands a
:class:`ServeConfig` to :class:`DaemonServer`.  The bind options are bundled in
one :class:`BindOptions` value object rather than threaded as a long parameter
list, and the CLI surface is a static command so the module stays class-first.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Self, final

import typer

from quarry.config import DEFAULT_PORT, Settings
from quarry.crash_logging import UncaughtExceptionLog
from quarry.daemon.bind_options import BindOptions
from quarry.daemon.server import DaemonServer, ServeConfig
from quarry.db_pointer import SELECTION
from quarry.installed_stop_timeout import InstalledStopTimeout
from quarry.logging_config import LoggingConfig
from quarry.net import LoopbackPolicy
from quarry.tls import TLS_DIR

# 256-bit URL-safe token — the loopback bearer minted when no key is supplied.
_TOKEN_BYTES = 32


@final
class DaemonLauncher:
    """Turn parsed :class:`BindOptions` into a running engine daemon."""

    _options: BindOptions

    def __new__(cls, options: BindOptions) -> Self:
        self = super().__new__(cls)
        self._options = options.normalized()
        return self

    def launch(self) -> None:
        """Refuse an unsafe bind, mint the loopback token, and serve."""
        options = self._options
        # Refuse a remote-reachable bind that has only an auto-minted token:
        # that token is unreadable by the remote clients who would need it, so
        # binding there without an operator-set key is false security.  The
        # guard runs against the ORIGINAL key, before the loopback fallback is
        # minted, so an auto-token can never satisfy a network bind.  A key
        # authenticates but does not encrypt, so a non-loopback bind must ALSO
        # carry TLS — else raw request content (transcripts) ships in cleartext.
        policy = LoopbackPolicy(options.host)
        policy.enforce_bind_key(options.api_key)
        policy.enforce_bind_tls(tls=options.tls)
        certfile, keyfile = self._tls_paths()
        config = ServeConfig(
            host=options.host,
            port=options.port,
            api_key=self._effective_key(),
            cors_origins=frozenset(options.cors_origins) or None,
            ssl_certfile=certfile,
            ssl_keyfile=keyfile,
        )
        settings = self._settings()
        InstalledStopTimeout.installed().report(settings.ingest_drain_timeout_s)
        DaemonServer.serve(settings, config)

    def _settings(self) -> Settings:
        name = self._options.db or SELECTION.persisted()
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
    """Console-script target: configure logging, then parse argv and launch.

    Logging is configured HERE rather than inside :meth:`DaemonLauncher.launch`
    so it covers the whole process: argument parsing, the bind-key and TLS
    refusals that exit before any server exists, and the uncaught-exception
    hooks.  Until this call the daemon had no logging configuration at all --
    root had no handlers, so Python fell back to ``logging.lastResort``, a
    bare stderr handler at WARNING with no formatter.  Every operational INFO
    line was discarded, and what did escape reached the supervisor's stderr
    file with no timestamp.

    ``stderr_level="INFO"`` keeps the supervisor's stderr file readable as a
    live tail; the rotating file is the record.
    """
    LoggingConfig.configure(stderr_level="INFO", log_file=LoggingConfig.DAEMON_LOG)
    UncaughtExceptionLog.install()
    typer.run(DaemonLauncher.cli)
