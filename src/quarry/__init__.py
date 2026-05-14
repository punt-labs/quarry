"""Quarry: local semantic search for your knowledge base.

Library API — import core types for programmatic use::

    from quarry import Settings, get_db, search
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
    from quarry.collections import derive_collection as derive_collection
    from quarry.config import Settings as Settings
    from quarry.database import get_db as get_db, search as search
    from quarry.pipeline import (
        ingest_content as ingest_content,
        ingest_document as ingest_document,
        ingest_url as ingest_url,
    )

__version__ = version("punt-quarry")

__all__ = [
    "Settings",
    "__version__",
    "derive_collection",
    "get_db",
    "ingest_content",
    "ingest_document",
    "ingest_url",
    "search",
]

# Map each public name to (module_path, attribute_name).
_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "derive_collection": ("quarry.collections", "derive_collection"),
    "Settings": ("quarry.config", "Settings"),
    "get_db": ("quarry.database", "get_db"),
    "search": ("quarry.database", "search"),
    "ingest_content": ("quarry.pipeline", "ingest_content"),
    "ingest_document": ("quarry.pipeline", "ingest_document"),
    "ingest_url": ("quarry.pipeline", "ingest_url"),
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
