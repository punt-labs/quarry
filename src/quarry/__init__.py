from __future__ import annotations

from importlib.metadata import version

from quarry.collections import derive_collection
from quarry.config import Settings, get_settings
from quarry.database import get_db, search
from quarry.pipeline import ingest_document, ingest_text

__version__ = version("quarry-mcp")

__all__ = [
    "Settings",
    "__version__",
    "derive_collection",
    "get_db",
    "get_settings",
    "ingest_document",
    "ingest_text",
    "search",
]
