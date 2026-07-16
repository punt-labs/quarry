"""The daemon process entry point: warm the engine, then serve over uvicorn.

``serve`` is the public entry the CLI calls; it bundles the bind options into a
:class:`ServeConfig` and drives a :class:`DaemonServer`, which warms the engine
single-threaded (DES-032) before accepting traffic and writes the actual bound
port to a file so ephemeral-port (``port=0``) callers can discover it.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Self, final

import uvicorn

from quarry.config import DEFAULT_PORT, Settings
from quarry.daemon.app import build_app
from quarry.daemon.context import DaemonContext
from quarry.fd_telemetry import FdTelemetry
from quarry.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from socket import socket

    from starlette.applications import Starlette

logger = logging.getLogger(__name__)

# Sample the daemon's open-fd count every 5 minutes so a climbing trend — the
# LanceDB deleted-index-handle leak — surfaces in logs before it reaches EMFILE.
_FD_TELEMETRY_INTERVAL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class ServeConfig:
    """The daemon's bind options: address, auth, CORS, and TLS material."""

    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    api_key: str | None = None
    cors_origins: frozenset[str] | None = None
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None

    @property
    def tls_enabled(self) -> bool:
        return self.ssl_certfile is not None and self.ssl_keyfile is not None

    @property
    def scheme(self) -> str:
        return "https" if self.tls_enabled else "http"


@final
class DaemonServer:
    """Warm the engine and serve the REST API until a shutdown signal."""

    _settings: Settings
    _config: ServeConfig
    _run_dir: RunDir
    _bound: bool

    def __new__(cls, settings: Settings, config: ServeConfig) -> Self:
        config = cls._authenticated(config)
        self = super().__new__(cls)
        self._settings = settings
        self._config = config
        self._run_dir = RunDir(settings.lancedb_path.parent)
        self._bound = False
        return self

    @staticmethod
    def _authenticated(config: ServeConfig) -> ServeConfig:
        """Normalize the API key and refuse to serve without one (R4).

        Two guarantees at the single daemon boundary every caller passes through:

        - Strip the key once so ``serve.token``, the auth comparison, and the
          loopback client all use the same value.  An operator key with a
          trailing newline (``QUARRY_API_KEY=$(cat keyfile)``) would otherwise
          authenticate the raw value while the loopback client presents the
          stripped ``serve.token`` --- a 401 on the operator's own machine.
        - Fail closed: EVERY bind, loopback included, must carry a key (R4:
          loopback => token-required).  The ``quarryd`` launcher mints a
          loopback token, but a caller who passes none is refused here rather
          than run open --- auth can never silently disable, regardless of
          caller.  A non-loopback bind with no operator key is already refused
          earlier by the launcher (before a token is minted), so an
          auto-minted token can never satisfy a network bind.
        """
        key = (config.api_key or "").strip() or None
        if key is None:
            msg = (
                "Refusing to serve without an API key. Every bind must be "
                "authenticated (the quarryd launcher mints a loopback token)."
            )
            raise SystemExit(msg)
        return replace(config, api_key=key)

    @classmethod
    def serve(cls, settings: Settings, config: ServeConfig) -> None:
        """Warm the engine and serve *config* until shutdown (process entry).

        The bind options arrive already bundled in a :class:`ServeConfig`, so
        the caller (the ``quarryd`` entry point) owns option parsing and this
        method stays a two-argument seam rather than a wide parameter list.
        """
        cls(settings, config).run()

    def run(self) -> None:
        """Build the app, bind uvicorn, and block until shutdown."""
        ctx = DaemonContext(
            self._settings,
            api_key=self._config.api_key,
            cors_origins=self._config.cors_origins,
        )
        ctx.warm()  # Build cached resources single-threaded before serving.

        app = build_app(ctx, lifespan=self._lifespan)
        server = uvicorn.Server(self._uvicorn_config(app))
        self._install_startup_hook(server)
        logger.info(
            "Starting Quarry server on %s:%d", self._config.host, self._config.port
        )
        server.run()
        logger.info("Server stopped")

    @asynccontextmanager
    async def _lifespan(self, _app: Starlette) -> AsyncGenerator[None]:
        # Runs for the daemon's lifetime; cancelled on shutdown below.
        monitor = asyncio.create_task(FdTelemetry(_FD_TELEMETRY_INTERVAL_SECONDS).run())
        try:
            yield
        finally:
            monitor.cancel()
            # Remove ONLY the sidecars this instance wrote after its own
            # successful bind.  A second quarryd that fails to bind (port in
            # use) must not delete a running peer's live serve.token / port
            # file on the shared path on its way out.
            if self._bound:
                self._run_dir.port_file.remove()
                self._run_dir.token_file.remove()
            with suppress(asyncio.CancelledError):
                await monitor

    def _uvicorn_config(self, app: Starlette) -> uvicorn.Config:
        return uvicorn.Config(
            app,
            host=self._config.host,
            port=self._config.port,
            log_config=None,
            log_level="warning",
            access_log=False,
            ssl_certfile=self._config.ssl_certfile,
            ssl_keyfile=self._config.ssl_keyfile,
        )

    def _install_startup_hook(self, server: uvicorn.Server) -> None:
        """Wrap uvicorn startup to write the sidecars AFTER a successful bind.

        Both ``serve.port`` (which also lets ``port=0`` callers discover the
        OS-assigned port) and ``serve.token`` are written only once the
        socket has bound.  A failed bind therefore never writes them, so a
        second instance racing an already-bound port cannot clobber a running
        peer's live token.  The write lands microseconds after the serve loop
        starts accepting; a client that races that window fails closed and
        retries --- an acceptable trade for not nuking a live peer.
        """
        original_startup = server.startup

        async def _startup_with_sidecars(sockets: list[socket] | None = None) -> None:
            await original_startup(sockets=sockets)
            if server.servers and server.servers[0].sockets:
                actual_port = server.servers[0].sockets[0].getsockname()[1]
                self._write_sidecars(actual_port)
                logger.info(
                    "Quarry server listening on %s://%s:%d",
                    self._config.scheme,
                    self._config.host,
                    actual_port,
                )
            else:
                logger.error(
                    "Server started but no bound sockets; sidecars not written"
                )

        server.startup = _startup_with_sidecars  # type: ignore[method-assign]

    def _write_sidecars(self, actual_port: int) -> None:
        """Write serve.token + serve.port all-or-nothing after a successful bind.

        On any write failure, remove ONLY the sidecars THIS instance wrote —
        never a peer's on the shared per-db path.  An instance with no api_key
        writes no token, so its failed port write must not delete a running
        peer's serve.token.  Both writes are atomic (a failed write leaves
        nothing), so the flags record only completed writes.  ``_bound`` is set
        only after both succeed, keeping the shutdown cleanup consistent.
        """
        wrote_token = False
        wrote_port = False
        try:
            if self._config.api_key is not None:
                self._run_dir.token_file.write(self._config.api_key)
                wrote_token = True
            self._run_dir.port_file.write(actual_port)
            wrote_port = True
        except OSError:
            if wrote_token:
                self._run_dir.token_file.remove()
            if wrote_port:
                self._run_dir.port_file.remove()
            raise
        self._bound = True
