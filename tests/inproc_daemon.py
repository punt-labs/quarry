"""In-process daemon for hermetic daemon-mandatory tests (DES-031).

Serves the REAL daemon FastAPI app through Starlette's ASGI ``TestClient`` — no
socket, no ONNX model — so a CLI/MCP test that would otherwise run ``resolve()``
and hit a LIVE ``quarryd`` instead drives the real request handlers in-process.
Patch ``TargetResolver.connect`` to return :meth:`InProcessDaemon.client` and the
whole ``CLI → QuarryClient → daemon → LanceDB`` round-trip runs against real code
on a real (empty) tmp database and passes with NO daemon running — the
"daemon-mandatory tests must be hermetic" rule, enforceable with the daemon
STOPPED.

The response path delegates to the production ``HttpxTransport._parse`` so the
hermetic transport cannot drift from the wire behaviour it stands in for
(bug-class-3): identical status→error mapping, body truncation, and JSON
handling as the shipped client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

import numpy as np

from quarry.client import QuarryClient
from quarry.client.transport import HttpxTransport, Response
from quarry.config import Settings
from quarry.daemon.app import build_app
from quarry.daemon.context import DaemonContext

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from fastapi import FastAPI
    from numpy.typing import NDArray
    from starlette.testclient import TestClient


@final
class FakeEmbedder:
    """A deterministic zero-vector embedder so the daemon skips the ONNX model.

    Satisfies ``quarry.types.EmbeddingBackend`` structurally; every vector is
    zeros of the configured dimension, which is all a hermetic route test needs
    (search over an empty table returns nothing regardless of the query vector).
    """

    _dimension: int

    def __new__(cls, dimension: int = 768) -> Self:
        self = super().__new__(cls)
        self._dimension = dimension
        return self

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return "fake-embedder"

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        return np.zeros((len(texts), self._dimension), dtype=np.float32)

    def embed_query(self, query: str) -> NDArray[np.float32]:
        return np.zeros(self._dimension, dtype=np.float32)


@final
class AsgiTransport:
    """A ``QuarryClient`` transport over the daemon app via ``TestClient``."""

    _client: TestClient

    def __new__(cls, client: TestClient) -> Self:
        self = super().__new__(cls)
        self._client = client
        return self

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        # timeout is irrelevant in-process (no socket); accepted for protocol parity.
        timeout: float | None = None,
    ) -> Response:
        """Route the request through the ASGI app and parse it as production does."""
        resp = self._client.request(
            method,
            path,
            params=dict(params) if params else None,
            json=dict(json_body) if json_body is not None else None,
        )
        # Reuse the shipped parser so the hermetic path matches the wire exactly.
        return HttpxTransport._parse(resp)


@final
class InProcessDaemon:
    """The real daemon app wired for hermetic tests: real handlers, tmp DB, no ONNX."""

    _ctx: DaemonContext
    _app: FastAPI

    def __new__(cls, data_dir: Path, *, api_key: str | None = None) -> Self:
        self = super().__new__(cls)
        (data_dir / "lancedb").mkdir(parents=True, exist_ok=True)
        settings = Settings(
            lancedb_path=data_dir / "lancedb",
            registry_path=data_dir / "registry.db",
        )
        # With api_key set the routes enforce bearer auth — the TLS smoke uses it
        # to exercise the authenticated wire; None (default) leaves routes open.
        # The fake embedder goes through DaemonContext's public DI seam, so no
        # ONNX session is ever built and no private resource internals are touched.
        ctx = DaemonContext(
            settings,
            api_key=api_key,
            embedder=FakeEmbedder(settings.embedding_dimension),
        )
        self._ctx = ctx
        self._app = build_app(ctx)
        return self

    @property
    def app(self) -> FastAPI:
        """The daemon's ASGI application (wrap with ``TestClient`` to drive it)."""
        return self._app

    @property
    def ctx(self) -> DaemonContext:
        """The daemon context, for tests that inspect tasks or the ingest queue."""
        return self._ctx

    def client(self, testclient: TestClient) -> QuarryClient:
        """Return a ``QuarryClient`` whose transport is this in-process app."""
        return QuarryClient(AsgiTransport(testclient))
