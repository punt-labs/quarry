"""Application settings.

Settings are grouped by concern: LanceDB paths, embedding model,
and chunking params.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic_settings import BaseSettings

ONNX_MODEL_REPO = "Snowflake/snowflake-arctic-embed-m-v1.5"
ONNX_MODEL_REVISION = "e58a8f756156a1293d763f17e3aae643474e9b8a"
ONNX_TOKENIZER_FILE = "tokenizer.json"
ONNX_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Settings(BaseSettings):
    quarry_root: Path = Path.home() / ".punt-labs" / "quarry" / "data"
    lancedb_path: Path = (
        Path.home() / ".punt-labs" / "quarry" / "data" / "default" / "lancedb"
    )
    registry_path: Path = (
        Path.home() / ".punt-labs" / "quarry" / "data" / "default" / "registry.db"
    )
    embedding_model: str = "Snowflake/snowflake-arctic-embed-m-v1.5"
    embedding_dimension: int = 768

    chunk_max_chars: int = 1800
    chunk_overlap_chars: int = 200

    model_config = {"env_file": ".env", "extra": "ignore"}


DEFAULT_PORT = 8420
"""Well-known port for ``quarry serve``.  Used by mcp-proxy configs and
service files (launchd, systemd) so the daemon URL is static."""

_DEFAULT_LANCEDB = (
    Path.home() / ".punt-labs" / "quarry" / "data" / "default" / "lancedb"
)


def resolve_db_paths(settings: Settings, db_name: str | None = None) -> Settings:
    """Return a copy of *settings* with lancedb_path and registry_path resolved.

    If *db_name* is provided, paths resolve to ``quarry_root / db_name / ...``.
    If ``LANCEDB_PATH`` was overridden (via env var or ``.env``), the caller's
    explicit path is preserved.
    When *db_name* is None and no override, paths use the ``default`` database.

    Raises ``ValueError`` if *db_name* contains path separators or traversal
    segments.
    """
    if db_name is not None and (
        "/" in db_name or "\\" in db_name or db_name in (".", "..")
    ):
        msg = f"Invalid database name: {db_name!r}"
        raise ValueError(msg)

    if settings.lancedb_path != _DEFAULT_LANCEDB:
        return settings

    name = db_name or "default"
    return settings.model_copy(
        update={
            "lancedb_path": settings.quarry_root / name / "lancedb",
            "registry_path": settings.quarry_root / name / "registry.db",
        },
    )


_CONFIG_PATH = Path.home() / ".punt-labs" / "quarry" / "config.toml"


def read_default_db() -> str | None:
    """Read the persistent default database name from config file."""
    if not _CONFIG_PATH.exists():
        return None
    text = _CONFIG_PATH.read_text()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    value = data.get("default", {}).get("database", "")
    if value and value != "default":
        return str(value)
    return None


def write_default_db(name: str) -> None:
    """Write the persistent default database name to config file."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = f'[default]\ndatabase = "{name}"\n'
    _CONFIG_PATH.write_text(content)


def load_settings() -> Settings:
    """Load application settings. Creates a fresh instance each call (no caching)."""
    return Settings()
