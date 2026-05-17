"""Quarry: local semantic search for your knowledge base.

Library API — import core types for programmatic use::

    from quarry import Settings, ChunkSearch, get_db
    from quarry import ingest_content, ingest_document, ingest_url

All public symbols are lazy-loaded via PEP 562 to avoid pulling in
pydantic, lancedb, onnxruntime, etc. when lightweight entry points
(``quarry-hook``) only need stdlib.
"""

from __future__ import annotations

import importlib
from importlib.metadata import version
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quarry.collections import CollectionName as CollectionName
    from quarry.config import Settings as Settings
    from quarry.db.chunk_search import ChunkSearch as ChunkSearch
    from quarry.db.facade import Database as Database
    from quarry.db.storage import get_db as get_db
    from quarry.ingestion.pipeline import (
        ingest_content as ingest_content,
        ingest_document as ingest_document,
        ingest_url as ingest_url,
    )

__version__ = version("punt-quarry")

__all__ = [
    "ChunkSearch",
    "CollectionName",
    "Database",
    "Settings",
    "__version__",
    "get_db",
    "ingest_content",
    "ingest_document",
    "ingest_url",
]

# Map each public name to (module_path, attribute_name).
_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "ChunkSearch": ("quarry.db.chunk_search", "ChunkSearch"),
    "Database": ("quarry.db.facade", "Database"),
    "CollectionName": ("quarry.collections", "CollectionName"),
    "Settings": ("quarry.config", "Settings"),
    "get_db": ("quarry.db.storage", "get_db"),
    "ingest_content": ("quarry.ingestion.pipeline", "ingest_content"),
    "ingest_document": ("quarry.ingestion.pipeline", "ingest_document"),
    "ingest_url": ("quarry.ingestion.pipeline", "ingest_url"),
}


def __getattr__(name: str) -> object:
    """Lazy import for public API symbols.

    Avoids loading the full dependency tree (lancedb, pydantic,
    onnxruntime) when lightweight entry points like ``quarry-hook``
    only need ``quarry._stdlib``.
    """
    if name in _LAZY_ATTRS:
        module_path, attr = _LAZY_ATTRS[name]
        mod = importlib.import_module(module_path)
        value = getattr(mod, attr)
        globals()[name] = value  # cache for subsequent access
        return value
    msg = f"module 'quarry' has no attribute {name!r}"
    raise AttributeError(msg)
