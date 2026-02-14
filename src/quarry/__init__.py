from __future__ import annotations

from importlib.metadata import version

from quarry.collections import derive_collection
from quarry.config import Settings, load_settings
from quarry.database import get_db, search
from quarry.pipeline import ingest_content, ingest_document

__version__ = version("quarry-mcp")

__all__ = [
    "Settings",
    "__version__",
    "derive_collection",
    "get_db",
    "ingest_content",
    "ingest_document",
    "load_settings",
    "search",
]
