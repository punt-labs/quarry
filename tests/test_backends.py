from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import quarry.embeddings as embeddings_mod
from quarry.backends import clear_caches, get_embedding_backend, get_ocr_backend
from quarry.config import Settings
from quarry.embeddings import SnowflakeEmbeddingBackend
from quarry.models import PageContent, PageType
from quarry.ocr_client import TextractOcrBackend
from quarry.ocr_local import LocalOcrBackend


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "aws_access_key_id": "test",
        "aws_secret_access_key": "test",
        "textract_poll_initial": 0,
        "textract_max_wait": 1,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class TestGetOcrBackend:
    def setup_method(self) -> None:
        clear_caches()

    def test_returns_local_backend_by_default(self) -> None:
        backend = get_ocr_backend(_settings())
        assert isinstance(backend, LocalOcrBackend)

    def test_returns_textract_backend(self) -> None:
        backend = get_ocr_backend(_settings(ocr_backend="textract"))
        assert isinstance(backend, TextractOcrBackend)

    def test_caches_by_key(self) -> None:
        settings = _settings()
        first = get_ocr_backend(settings)
        second = get_ocr_backend(settings)
        assert first is second

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown OCR backend: 'nope'"):
            get_ocr_backend(_settings(ocr_backend="nope"))


class TestGetEmbeddingBackend:
    def setup_method(self) -> None:
        clear_caches()

    def test_returns_snowflake_backend(self) -> None:
        backend = get_embedding_backend(_settings())
        assert isinstance(backend, SnowflakeEmbeddingBackend)

    def test_exposes_dimension(self) -> None:
        backend = get_embedding_backend(_settings())
        assert backend.dimension == 768

    def test_exposes_model_name(self) -> None:
        backend = get_embedding_backend(_settings())
        assert backend.model_name == "Snowflake/snowflake-arctic-embed-m-v1.5"

    def test_caches_by_model(self) -> None:
        settings = _settings()
        first = get_embedding_backend(settings)
        second = get_embedding_backend(settings)
        assert first is second


class TestClearCaches:
    def test_clears_both_caches(self) -> None:
        settings = _settings()
        get_ocr_backend(settings)
        get_embedding_backend(settings)
        clear_caches()
        # After clearing, new instances should be created
        backend = get_ocr_backend(settings)
        assert isinstance(backend, LocalOcrBackend)


class TestTextractOcrBackend:
    def setup_method(self) -> None:
        clear_caches()

    def test_ocr_document_delegates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pdf_file = tmp_path / "test.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        expected = [
            PageContent(
                document_name="test.pdf",
                document_path=str(pdf_file.resolve()),
                page_number=1,
                total_pages=1,
                text="OCR text",
                page_type=PageType.IMAGE,
            )
        ]
        monkeypatch.setattr(
            "quarry.ocr_client.ocr_document_via_s3",
            lambda _path, _pages, _total, _settings, **_kw: expected,
        )

        backend = get_ocr_backend(_settings(ocr_backend="textract"))
        result = backend.ocr_document(pdf_file, [1], 1, document_name="test.pdf")
        assert result == expected

    def test_ocr_image_bytes_delegates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        expected = PageContent(
            document_name="img.png",
            document_path="/tmp/img.png",
            page_number=1,
            total_pages=1,
            text="Image text",
            page_type=PageType.IMAGE,
        )
        monkeypatch.setattr(
            "quarry.ocr_client.ocr_image_bytes",
            lambda _bytes, _name, _path: expected,
        )

        backend = get_ocr_backend(_settings(ocr_backend="textract"))
        result = backend.ocr_image_bytes(b"fake", "img.png", "/tmp/img.png")
        assert result == expected


class TestSnowflakeEmbeddingBackend:
    def setup_method(self) -> None:
        clear_caches()
        embeddings_mod._models.clear()

    def test_embed_texts_delegates(self) -> None:
        model = MagicMock()
        expected = np.random.default_rng(0).standard_normal((3, 768)).astype(np.float32)
        model.encode.return_value = expected

        with patch("sentence_transformers.SentenceTransformer", return_value=model):
            backend = get_embedding_backend(_settings())
            result = backend.embed_texts(["a", "b", "c"])

        np.testing.assert_array_equal(result, expected)

    def test_embed_query_delegates(self) -> None:
        model = MagicMock()
        expected = np.zeros(768, dtype=np.float32)
        model.encode.return_value = expected

        with patch("sentence_transformers.SentenceTransformer", return_value=model):
            backend = get_embedding_backend(_settings())
            result = backend.embed_query("search term")

        np.testing.assert_array_equal(result, expected)
        model.encode.assert_called_once_with(
            "search term",
            prompt_name="query",
            normalize_embeddings=True,
            show_progress_bar=False,
        )
