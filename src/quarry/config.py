"""Application settings: LanceDB paths, embedding model, and chunking."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import ClassVar

from pydantic import Field
from pydantic_settings import BaseSettings

ONNX_MODEL_REPO = "Snowflake/snowflake-arctic-embed-m-v1.5"
ONNX_MODEL_REVISION = "e58a8f756156a1293d763f17e3aae643474e9b8a"
ONNX_TOKENIZER_FILE = "tokenizer.json"
ONNX_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Settings(BaseSettings):
    quarry_root: Path = Path.home() / ".punt-labs" / "quarry" / "data"
    lancedb_path: Path = quarry_root / "default" / "lancedb"
    registry_path: Path = quarry_root / "default" / "registry.db"
    embedding_model: str = "Snowflake/snowflake-arctic-embed-m-v1.5"
    embedding_dimension: int = 768

    chunk_max_chars: int = 1800
    chunk_overlap_chars: int = 200

    # Bounded progressive commit (DES-034); embed_window_chunks is a kpz seam.
    # Both are >= 1: ProgressiveIndexer rejects a non-positive flush budget, so an
    # invalid value must fail loud at construction, not deep in the ingestor.
    sync_flush_mb: int = Field(default=32, ge=1)
    embed_window_chunks: int = Field(default=512, ge=1)

    # Serialized capture/index queue (DES-042).  embed_concurrency is clamped to
    # the queue's hard ceiling regardless of this value; queue_depth bounds the
    # admitted (in-flight + waiting) jobs; drain_timeout_s bounds the shutdown
    # drain.  The collection key is client-controlled, so max_workers caps
    # resident workers and worker_idle_s reaps idle ones.
    ingest_embed_concurrency: int = Field(default=1, ge=1)
    ingest_queue_depth: int = Field(default=32, ge=1)
    ingest_drain_timeout_s: float = Field(default=30.0, ge=0)
    ingest_max_workers: int = Field(default=256, ge=1)
    ingest_worker_idle_s: float = Field(default=60.0, ge=0)

    model_config = {"env_file": ".env", "extra": "ignore"}

    _DEFAULT_LANCEDB: ClassVar[Path] = quarry_root / "default" / "lancedb"

    _CONFIG_PATH: ClassVar[Path] = Path.home() / ".punt-labs" / "quarry" / "config.toml"

    # The current process's --db override, recorded by the CLI so the client
    # tier resolves the daemon's startup-db run dir (where serve.token lives)
    # the same way the CLI resolves its own data — client and daemon agree on
    # the database by a matching --db.
    _active_db: ClassVar[str] = ""

    def resolve_db_paths(self, db_name: str | None = None) -> Settings:
        """Return a copy with lancedb_path and registry_path resolved.

        With *db_name*, paths resolve under ``quarry_root / db_name``. An explicit
        ``LANCEDB_PATH`` override is preserved; otherwise the ``default`` database
        is used. Raises ``ValueError`` if *db_name* contains path separators or
        traversal segments.
        """
        if db_name is not None and (
            "/" in db_name or "\\" in db_name or db_name in (".", "..")
        ):
            msg = f"Invalid database name: {db_name!r}"
            raise ValueError(msg)

        if self.lancedb_path != Settings._DEFAULT_LANCEDB:
            return self

        name = db_name or "default"
        return self.model_copy(
            update={
                "lancedb_path": self.quarry_root / name / "lancedb",
                "registry_path": self.quarry_root / name / "registry.db",
            },
        )

    @classmethod
    def read_default_db(cls) -> str | None:
        """Read the persistent default database name from config file."""
        if not cls._CONFIG_PATH.exists():
            return None
        text = cls._CONFIG_PATH.read_text()
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return None
        value = data.get("default", {}).get("database", "")
        if value and value != "default":
            return str(value)
        return None

    @classmethod
    def write_default_db(cls, name: str) -> None:
        """Write the persistent default database name to config file."""
        cls._CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        content = f'[default]\ndatabase = "{name}"\n'
        cls._CONFIG_PATH.write_text(content)

    @classmethod
    def set_active_db(cls, name: str) -> None:
        """Record this process's ``--db`` override for db resolution."""
        cls._active_db = name

    @classmethod
    def active_db(cls) -> str | None:
        """Return the effective database: ``--db`` override, else the default."""
        return cls._active_db or cls.read_default_db()

    @classmethod
    def load(cls) -> Settings:
        """Load application settings. Fresh instance each call."""
        return cls()


DEFAULT_PORT = 8420  # well-known port for ``quarryd`` + mcp-proxy configs
