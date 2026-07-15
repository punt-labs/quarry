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

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path
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
class PortFile:
    """The ``serve.port`` sidecar: the daemon's actual bound port for callers."""

    _path: Path

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        self._path = path
        return self

    def write(self, port: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(str(port))
        logger.info("Wrote port file: %s (port %d)", self._path, port)

    def remove(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
            logger.info("Removed port file: %s", self._path)
        except OSError:
            logger.warning("Could not remove port file: %s", self._path)


@final
class DaemonServer:
    """Warm the engine and serve the REST API until a shutdown signal."""

    _settings: Settings
    _config: ServeConfig
    _port_file: PortFile

    def __new__(cls, settings: Settings, config: ServeConfig) -> Self:
        cls._validate_host_key(config.host, config.api_key)
        self = super().__new__(cls)
        self._settings = settings
        self._config = config
        self._port_file = PortFile(settings.lancedb_path.parent / "serve.port")
        return self

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
        self._install_port_file_hook(server)

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
            # Remove the port file first so cleanup is guaranteed even if
            # draining the monitor task surfaces an error.
            monitor.cancel()
            self._port_file.remove()
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
        port_file = self._port_file

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

    @staticmethod
    def _validate_host_key(host: str, api_key: str | None) -> None:
        """Refuse to bind to non-loopback without an API key."""
        if host != "127.0.0.1" and not api_key:
            msg = (
                "Refusing to bind to %s without --api-key. "
                "Non-loopback hosts require authentication."
            )
            raise SystemExit(msg % host)


def serve(
    settings: Settings,
    port: int = DEFAULT_PORT,
    *,
    host: str = "127.0.0.1",
    api_key: str | None = None,
    cors_origins: frozenset[str] | None = None,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
) -> None:
    """Start the daemon HTTP server.  Blocks until shutdown (CLI entry point)."""
    config = ServeConfig(
        host=host,
        port=port,
        api_key=api_key,
        cors_origins=cors_origins,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )
    DaemonServer(settings, config).run()
