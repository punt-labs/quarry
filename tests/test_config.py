from __future__ import annotations

from pathlib import Path

from quarry import __version__
from quarry.config import Settings


class TestVersion:
    def test_version_is_string(self):
        assert isinstance(__version__, str)

    def test_version_format(self):
        parts = __version__.split(".")
        assert len(parts) == 3
        assert all(part.isdigit() for part in parts)


class TestSettings:
    def test_defaults(self):
        settings = Settings(
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        assert settings.aws_default_region == "us-east-1"
        assert settings.s3_bucket == "ocr-7f3a1b2e4c5d4e8f9a1b3c5d7e9f2a4b"
        assert settings.chunk_max_chars == 1800
        assert settings.chunk_overlap_chars == 200
        assert settings.textract_poll_interval == 5
        assert settings.textract_max_wait == 900
        assert isinstance(settings.lancedb_path, Path)

    def test_override_via_constructor(self):
        settings = Settings(
            aws_access_key_id="key",
            aws_secret_access_key="secret",
            aws_default_region="eu-west-1",
            chunk_max_chars=1000,
        )
        assert settings.aws_default_region == "eu-west-1"
        assert settings.chunk_max_chars == 1000

    def test_override_via_env(self, monkeypatch):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "env-key")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-secret")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-1")
        settings = Settings()
        assert settings.aws_access_key_id == "env-key"
        assert settings.aws_default_region == "ap-southeast-1"

    def test_default_lancedb_path_under_home(self):
        settings = Settings(
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        home = Path.home()
        assert settings.lancedb_path == home / ".quarry" / "data" / "lancedb"

    def test_embedding_model_default(self):
        settings = Settings(
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        assert settings.embedding_model == "Snowflake/snowflake-arctic-embed-m-v1.5"
