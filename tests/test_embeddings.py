from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np

from quarry.embeddings import OnnxEmbeddingBackend


def _mock_session() -> MagicMock:
    """Create a mock ONNX InferenceSession that returns zeros."""
    session = MagicMock()
    # Shape: (batch, seq_len, hidden_dim) — CLS token at index 0
    session.run.return_value = (np.zeros((1, 5, 768), dtype=np.float32),)
    return session


def _mock_tokenizer() -> MagicMock:
    """Create a mock tokenizer returning fixed-length encodings."""
    tokenizer = MagicMock()

    def _make_encoding() -> MagicMock:
        enc = MagicMock()
        enc.ids = [101, 2023, 2003, 1037, 102]
        enc.attention_mask = [1, 1, 1, 1, 1]
        return enc

    tokenizer.encode_batch.side_effect = lambda texts: [_make_encoding() for _ in texts]
    return tokenizer


def _patch_onnx_backend(session: MagicMock, tokenizer: MagicMock):
    """Patch huggingface_hub, tokenizers, and onnxruntime for unit tests."""
    return (
        patch(
            "quarry.embeddings._load_model_files",
            return_value=("/fake/model.onnx", "/fake/tokenizer.json"),
        ),
        patch("tokenizers.Tokenizer.from_file", return_value=tokenizer),
        patch("onnxruntime.InferenceSession", return_value=session),
    )


class TestEmbedTexts:
    def test_returns_embeddings(self):
        session = _mock_session()
        tokenizer = _mock_tokenizer()
        hidden = (
            np.random.default_rng(0).standard_normal((3, 5, 768)).astype(np.float32)
        )
        session.run.return_value = (hidden,)

        p1, p2, p3 = _patch_onnx_backend(session, tokenizer)
        with p1, p2, p3:
            backend = OnnxEmbeddingBackend()
            result = backend.embed_texts(["a", "b", "c"])

        assert result.shape == (3, 768)
        # Verify normalization: each vector should have unit norm
        norms = np.linalg.norm(result, axis=1)
        np.testing.assert_allclose(norms, 1.0, atol=1e-6)

    def test_cls_pooling(self):
        """Verify CLS token (index 0) is used for pooling."""
        session = _mock_session()
        tokenizer = _mock_tokenizer()
        hidden = np.zeros((1, 5, 768), dtype=np.float32)
        # Set CLS token to a known vector
        hidden[0, 0, :] = 1.0
        session.run.return_value = (hidden,)

        p1, p2, p3 = _patch_onnx_backend(session, tokenizer)
        with p1, p2, p3:
            backend = OnnxEmbeddingBackend()
            result = backend.embed_texts(["test"])

        # After normalization, should be 1/sqrt(768) in each dimension
        expected_val = 1.0 / np.sqrt(768)
        np.testing.assert_allclose(result[0], expected_val, atol=1e-6)

    def test_empty_texts_returns_empty_array(self):
        session = _mock_session()
        tokenizer = _mock_tokenizer()

        p1, p2, p3 = _patch_onnx_backend(session, tokenizer)
        with p1, p2, p3:
            backend = OnnxEmbeddingBackend()
            result = backend.embed_texts([])

        assert result.shape == (0, 768)
        session.run.assert_not_called()


class TestEmbedQuery:
    def test_prepends_query_prefix(self):
        session = _mock_session()
        tokenizer = _mock_tokenizer()

        p1, p2, p3 = _patch_onnx_backend(session, tokenizer)
        with p1, p2, p3:
            backend = OnnxEmbeddingBackend()
            backend.embed_query("search term")

        tokenizer.encode_batch.assert_called_once()
        texts = tokenizer.encode_batch.call_args[0][0]
        assert texts == [
            "Represent this sentence for searching relevant passages: search term"
        ]

    def test_returns_1d_vector(self):
        session = _mock_session()
        tokenizer = _mock_tokenizer()

        p1, p2, p3 = _patch_onnx_backend(session, tokenizer)
        with p1, p2, p3:
            backend = OnnxEmbeddingBackend()
            result = backend.embed_query("search term")

        assert result.shape == (768,)


class TestModelName:
    def test_returns_repo_name(self):
        session = _mock_session()
        tokenizer = _mock_tokenizer()

        p1, p2, p3 = _patch_onnx_backend(session, tokenizer)
        with p1, p2, p3:
            backend = OnnxEmbeddingBackend()

        assert backend.model_name == "Snowflake/snowflake-arctic-embed-m-v1.5"
