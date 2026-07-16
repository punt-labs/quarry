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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

import uvicorn

from quarry.config import DEFAULT_PORT, Settings
from quarry.daemon.app import build_app
from quarry.daemon.context import DaemonContext
from quarry.fd_telemetry import FdTelemetry
from quarry.net import LoopbackPolicy
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

    def __new__(cls, settings: Settings, config: ServeConfig) -> Self:
        LoopbackPolicy(config.host).enforce_bind_key(config.api_key)
        self = super().__new__(cls)
        self._settings = settings
        self._config = config
        self._run_dir = RunDir(settings.lancedb_path.parent)
        return self

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

        self._write_serve_token()
        try:
            app = build_app(ctx, lifespan=self._lifespan)
            server = uvicorn.Server(self._uvicorn_config(app))
            self._install_port_file_hook(server)
            logger.info(
                "Starting Quarry server on %s:%d",
                self._config.host,
                self._config.port,
            )
            server.run()
        finally:
            # Remove the token on ANY exit.  It is written BEFORE the socket
            # binds (so a client racing the first request can read it), so a
            # bind/startup failure after the write would otherwise leave a
            # stale token — defeating the client's missing-file fail-closed
            # check, making a DOWN daemon look like it holds a credential.
            self._run_dir.token_file.remove()
        logger.info("Server stopped")

    def _write_serve_token(self) -> None:
        """Persist the loopback bearer before the socket opens.

        The daemon now requires this bearer on loopback, so it must be on
        disk before the first request can race in.  A ``None`` key is the
        interim unauthenticated path (no token, no gate) — retired once
        every start goes through ``quarryd``, which always sets a key.
        """
        if self._config.api_key is not None:
            self._run_dir.token_file.write(self._config.api_key)

    @asynccontextmanager
    async def _lifespan(self, _app: Starlette) -> AsyncGenerator[None]:
        # Runs for the daemon's lifetime; cancelled on shutdown below.
        monitor = asyncio.create_task(FdTelemetry(_FD_TELEMETRY_INTERVAL_SECONDS).run())
        try:
            yield
        finally:
            # Remove the port file first so cleanup is guaranteed even if
            # draining the monitor task surfaces an error.
            monitor.cancel()
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

    def _install_port_file_hook(self, server: uvicorn.Server) -> None:
        """Wrap uvicorn startup to write the actual bound port after binding.

        Writing after bind is what lets ``port=0`` callers discover the
        OS-assigned ephemeral port.
        """
        original_startup = server.startup
        config = self._config
        port_file = self._run_dir.port_file

        async def _startup_with_port_file(sockets: list[socket] | None = None) -> None:
            await original_startup(sockets=sockets)
            if server.servers and server.servers[0].sockets:
                actual_port = server.servers[0].sockets[0].getsockname()[1]
                port_file.write(actual_port)
                logger.info(
                    "Quarry server listening on %s://%s:%d",
                    config.scheme,
                    config.host,
                    actual_port,
                )
            else:
                logger.error(
                    "Server started but no bound sockets; port file not written"
                )

        server.startup = _startup_with_port_file  # type: ignore[method-assign]
