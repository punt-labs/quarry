from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from quarry.backends import clear_caches, get_embedding_backend, get_ocr_backend
from quarry.config import Settings
from quarry.embeddings import OnnxEmbeddingBackend
from quarry.embeddings_sagemaker import SageMakerEmbeddingBackend
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
    return Settings.model_validate(defaults)


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


def _embedding_backend_patches() -> tuple[
    AbstractContextManager[object],
    AbstractContextManager[object],
    AbstractContextManager[object],
]:
    """Patches so get_embedding_backend() works without downloaded ONNX model."""
    session = MagicMock()
    rng = np.random.default_rng(0)
    session.run.return_value = (
        rng.standard_normal((1, 5, 768)).astype(np.float32),
        rng.standard_normal((1, 768)).astype(np.float32),
    )
    tokenizer = MagicMock()
    enc = MagicMock()
    enc.ids = [101, 2023, 2003, 1037, 102]
    enc.attention_mask = [1, 1, 1, 1, 1]
    tokenizer.encode_batch.side_effect = lambda texts: [enc for _ in texts]
    return (
        patch(
            "quarry.embeddings._load_model_files",
            return_value=("/fake/model.onnx", "/fake/tokenizer.json"),
        ),
        patch("tokenizers.Tokenizer.from_file", return_value=tokenizer),
        patch("onnxruntime.InferenceSession", return_value=session),
    )


class TestGetEmbeddingBackend:
    def setup_method(self) -> None:
        clear_caches()

    def test_returns_onnx_backend(self) -> None:
        p1, p2, p3 = _embedding_backend_patches()
        with p1, p2, p3:
            backend = get_embedding_backend(_settings())
        assert isinstance(backend, OnnxEmbeddingBackend)

    def test_exposes_dimension(self) -> None:
        p1, p2, p3 = _embedding_backend_patches()
        with p1, p2, p3:
            backend = get_embedding_backend(_settings())
        assert backend.dimension == 768

    def test_exposes_model_name(self) -> None:
        p1, p2, p3 = _embedding_backend_patches()
        with p1, p2, p3:
            backend = get_embedding_backend(_settings())
        assert backend.model_name == "Snowflake/snowflake-arctic-embed-m-v1.5"

    def test_caches_by_model(self) -> None:
        p1, p2, p3 = _embedding_backend_patches()
        with p1, p2, p3:
            settings = _settings()
            first = get_embedding_backend(settings)
            second = get_embedding_backend(settings)
        assert first is second

    def test_returns_sagemaker_backend(self) -> None:
        p1, p2, p3 = _embedding_backend_patches()
        with p1, p2, p3, patch("boto3.client"):
            backend = get_embedding_backend(
                _settings(
                    embedding_backend="sagemaker",
                    sagemaker_endpoint_name="test-ep",
                )
            )
        assert isinstance(backend, SageMakerEmbeddingBackend)

    def test_unknown_embedding_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown embedding backend: 'nope'"):
            get_embedding_backend(_settings(embedding_backend="nope"))

    def test_cache_distinguishes_backends(self) -> None:
        p1, p2, p3 = _embedding_backend_patches()
        with p1, p2, p3, patch("boto3.client"):
            onnx = get_embedding_backend(_settings(embedding_backend="onnx"))
            sm = get_embedding_backend(
                _settings(
                    embedding_backend="sagemaker",
                    sagemaker_endpoint_name="test-ep",
                )
            )
        assert type(onnx) is not type(sm)


class TestClearCaches:
    def test_clears_both_caches(self) -> None:
        p1, p2, p3 = _embedding_backend_patches()
        with p1, p2, p3:
            settings = _settings()
            get_ocr_backend(settings)
            get_embedding_backend(settings)
        clear_caches()
        # After clearing, new instances should be created
        backend = get_ocr_backend(settings)
        assert isinstance(backend, LocalOcrBackend)


@pytest.mark.slow
def test_embedding_backend_loads_real_model_when_cached() -> None:
    """Smoke test: real ONNX model loads when in HuggingFace cache.

    Skips when model is not cached (e.g. CI). Run with `pytest -m slow` or
    `quarry install` first to populate cache. Verifies load only; inference
    is covered by integration tests.
    """
    from huggingface_hub.errors import LocalEntryNotFoundError

    clear_caches()
    try:
        backend = get_embedding_backend(_settings())
    except LocalEntryNotFoundError as e:
        pytest.skip(f"ONNX model not in cache: {e}. Run 'quarry install' to download.")
    assert isinstance(backend, OnnxEmbeddingBackend)
    assert backend.dimension == 768
    assert backend.model_name == "Snowflake/snowflake-arctic-embed-m-v1.5"


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
        result = backend.ocr_image_bytes(b"fake", "img.png", Path("/tmp/img.png"))
        assert result == expected


class TestOnnxEmbeddingBackend:
    def setup_method(self) -> None:
        clear_caches()

    def _mock_onnx(self) -> tuple[MagicMock, MagicMock]:
        session = MagicMock()
        rng = np.random.default_rng(0)
        session.run.return_value = (
            rng.standard_normal((1, 5, 768)).astype(np.float32),
            rng.standard_normal((1, 768)).astype(np.float32),
        )
        tokenizer = MagicMock()
        enc = MagicMock()
        enc.ids = [101, 2023, 2003, 1037, 102]
        enc.attention_mask = [1, 1, 1, 1, 1]
        tokenizer.encode_batch.side_effect = lambda texts: [enc for _ in texts]
        return session, tokenizer

    def test_embed_texts_returns_correct_shape(self) -> None:
        session, tokenizer = self._mock_onnx()
        rng = np.random.default_rng(0)
        token_emb = rng.standard_normal((3, 5, 768)).astype(np.float32)
        sentence_emb = rng.standard_normal((3, 768)).astype(np.float32)
        session.run.return_value = (token_emb, sentence_emb)

        with (
            patch(
                "quarry.embeddings._load_model_files",
                return_value=("/fake/model.onnx", "/fake/tokenizer.json"),
            ),
            patch("tokenizers.Tokenizer.from_file", return_value=tokenizer),
            patch("onnxruntime.InferenceSession", return_value=session),
        ):
            backend = get_embedding_backend(_settings())
            result = backend.embed_texts(["a", "b", "c"])

        assert result.shape == (3, 768)

    def test_embed_query_applies_prefix(self) -> None:
        session, tokenizer = self._mock_onnx()

        with (
            patch(
                "quarry.embeddings._load_model_files",
                return_value=("/fake/model.onnx", "/fake/tokenizer.json"),
            ),
            patch("tokenizers.Tokenizer.from_file", return_value=tokenizer),
            patch("onnxruntime.InferenceSession", return_value=session),
        ):
            backend = get_embedding_backend(_settings())
            result = backend.embed_query("search term")

        assert result.shape == (768,)
        texts = tokenizer.encode_batch.call_args[0][0]
        assert texts[0].startswith(
            "Represent this sentence for searching relevant passages: "
        )
