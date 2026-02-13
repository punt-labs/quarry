"""Application settings (AWS, embedding, chunking, OCR) and logging config."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pydantic_settings import BaseSettings

ONNX_MODEL_REPO = "Snowflake/snowflake-arctic-embed-m-v1.5"
ONNX_MODEL_REVISION = "e58a8f756156a1293d763f17e3aae643474e9b8a"
ONNX_MODEL_FILE = "onnx/model_int8.onnx"
ONNX_TOKENIZER_FILE = "tokenizer.json"
ONNX_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Settings(BaseSettings):
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_default_region: str = "us-east-1"
    s3_bucket: str = ""

    lancedb_path: Path = Path.home() / ".quarry" / "data" / "lancedb"
    registry_path: Path = Path.home() / ".quarry" / "data" / "registry.db"
    log_path: Path = Path.home() / ".quarry" / "data" / "quarry.log"
    ocr_backend: str = "local"
    # Cache key for get_embedding_backend(); OnnxEmbeddingBackend ignores it.
    embedding_model: str = "Snowflake/snowflake-arctic-embed-m-v1.5"
    embedding_dimension: int = 768

    chunk_max_chars: int = 1800
    chunk_overlap_chars: int = 200

    textract_poll_initial: float = 5.0
    textract_poll_max: float = 30.0
    textract_max_wait: int = 900
    textract_max_image_bytes: int = 10_485_760  # 10 MiB sync API limit

    model_config = {"env_file": ".env", "extra": "ignore"}


def get_settings() -> Settings:
    return Settings()


def configure_logging(settings: Settings) -> None:
    """Set up root logger with stderr and file handlers at INFO level.

    Idempotent: returns early if root logger already has handlers.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    root.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")

    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)

    file_handler = RotatingFileHandler(
        settings.log_path,
        maxBytes=5_000_000,  # 5 MB per file
        backupCount=3,  # keep quarry.log.1, .2, .3
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
