from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_default_region: str = "us-east-1"
    s3_bucket: str = "ocr-7f3a1b2e4c5d4e8f9a1b3c5d7e9f2a4b"

    lancedb_path: Path = (
        Path(__file__).resolve().parent.parent.parent / "data" / "lancedb"
    )
    embedding_model: str = "Snowflake/snowflake-arctic-embed-m-v1.5"

    chunk_max_chars: int = 1800
    chunk_overlap_chars: int = 200

    textract_poll_interval: int = 5
    textract_max_wait: int = 900

    model_config = {"env_file": ".env", "extra": "ignore"}


def get_settings() -> Settings:
    return Settings()
