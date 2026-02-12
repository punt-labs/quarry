from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np

import quarry.embeddings as embeddings_mod
from quarry.embeddings import embed_query, embed_texts


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
            "quarry.embeddings._download_model_files",
            return_value=("/fake/model.onnx", "/fake/tokenizer.json"),
        ),
        patch("tokenizers.Tokenizer.from_file", return_value=tokenizer),
        patch("onnxruntime.InferenceSession", return_value=session),
    )


class TestEmbedTexts:
    def setup_method(self):
        embeddings_mod._backends.clear()

    def test_returns_embeddings(self):
        session = _mock_session()
        tokenizer = _mock_tokenizer()
        hidden = (
            np.random.default_rng(0).standard_normal((3, 5, 768)).astype(np.float32)
        )
        session.run.return_value = (hidden,)

        p1, p2, p3 = _patch_onnx_backend(session, tokenizer)
        with p1, p2, p3:
            result = embed_texts(["a", "b", "c"])

        assert result.shape == (3, 768)
        # Verify normalization: each vector should have unit norm
        norms = np.linalg.norm(result, axis=1)
        np.testing.assert_allclose(norms, 1.0, atol=1e-6)

    def test_caches_backend(self):
        session = _mock_session()
        tokenizer = _mock_tokenizer()

        p1, p2, p3 = _patch_onnx_backend(session, tokenizer)
        with p1 as mock_download, p2, p3:
            embed_texts(["a"])
            embed_texts(["b"])

        mock_download.assert_called_once()

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
            result = embed_texts(["test"])

        # After normalization, should be 1/sqrt(768) in each dimension
        expected_val = 1.0 / np.sqrt(768)
        np.testing.assert_allclose(result[0], expected_val, atol=1e-6)


class TestEmbedQuery:
    def setup_method(self):
        embeddings_mod._backends.clear()

    def test_prepends_query_prefix(self):
        session = _mock_session()
        tokenizer = _mock_tokenizer()

        p1, p2, p3 = _patch_onnx_backend(session, tokenizer)
        with p1, p2, p3:
            embed_query("search term")

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
            result = embed_query("search term")

        assert result.shape == (768,)
