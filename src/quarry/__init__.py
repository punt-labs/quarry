"""Quarry: local semantic search — the thin-client library.

Import the client surface for programmatic use::

    import quarry
    from quarry.api import IngestRequest, SearchRequest

    client = quarry.TargetResolver.connect()   # resolves the local daemon
    resp = client.search(SearchRequest(query="what did we decide about X"))

The library holds no engine: using it never pulls lancedb, onnxruntime, or the
ingestion pipeline into the caller — those live only in the daemon. Request and
response models live in ``quarry.api``; the engine's own modules (``quarry.db``,
``quarry.ingestion.pipeline``, ``quarry.retrieval``) stay importable for
server-side use but are not re-exported here. Names are lazy-loaded via PEP 562
so a bare ``import quarry`` stays stdlib-cheap on the ``quarry-hook`` path — the
pydantic/httpx client cost is paid only on first attribute access.
"""

from __future__ import annotations

import importlib
from importlib.metadata import version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quarry.client import (
        ClientConfig as ClientConfig,
        HttpError as HttpError,
        QuarryClient as QuarryClient,
        QuarryConnectionError as QuarryConnectionError,
        QuarryError as QuarryError,
        TargetResolver as TargetResolver,
        TaskOutcome as TaskOutcome,
    )

__version__ = version("punt-quarry")

__all__ = [
    "ClientConfig",
    "HttpError",
    "QuarryClient",
    "QuarryConnectionError",
    "QuarryError",
    "TargetResolver",
    "TaskOutcome",
    "__version__",
]

# Each public name maps to (module_path, attribute) in the client tier.
_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "ClientConfig": ("quarry.client", "ClientConfig"),
    "HttpError": ("quarry.client", "HttpError"),
    "QuarryClient": ("quarry.client", "QuarryClient"),
    "QuarryConnectionError": ("quarry.client", "QuarryConnectionError"),
    "QuarryError": ("quarry.client", "QuarryError"),
    "TargetResolver": ("quarry.client", "TargetResolver"),
    "TaskOutcome": ("quarry.client", "TaskOutcome"),
}


def __getattr__(name: str) -> object:
    """Lazily resolve a public client name via PEP 562 (see module docstring)."""
    if name not in _LAZY_ATTRS:
        msg = f"module 'quarry' has no attribute {name!r}"
        raise AttributeError(msg)
    module_path, attr = _LAZY_ATTRS[name]
    value = getattr(importlib.import_module(module_path), attr)
    globals()[name] = value  # cache so the client import happens at most once
    return value
