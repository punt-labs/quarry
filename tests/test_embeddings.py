from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np

import quarry.embeddings as embeddings_mod
from quarry.config import EMBEDDING_MODEL_REVISION
from quarry.embeddings import embed_query, embed_texts


def _mock_model() -> MagicMock:
    model = MagicMock()
    model.encode.return_value = np.zeros((1, 768), dtype=np.float32)
    return model


class TestEmbedTexts:
    def setup_method(self):
        embeddings_mod._models.clear()

    def test_returns_embeddings(self):
        model = _mock_model()
        expected = np.random.default_rng(0).standard_normal((3, 768)).astype(np.float32)
        model.encode.return_value = expected

        with patch(
            "sentence_transformers.SentenceTransformer", return_value=model
        ) as mock_cls:
            result = embed_texts(["a", "b", "c"])

        np.testing.assert_array_equal(result, expected)
        mock_cls.assert_called_once_with(
            "Snowflake/snowflake-arctic-embed-m-v1.5",
            revision=EMBEDDING_MODEL_REVISION,
            local_files_only=True,
        )
        model.encode.assert_called_once_with(
            ["a", "b", "c"],
            normalize_embeddings=True,
            show_progress_bar=False,
        )

    def test_caches_model(self):
        model = _mock_model()
        with patch(
            "sentence_transformers.SentenceTransformer", return_value=model
        ) as mock_cls:
            embed_texts(["a"])
            embed_texts(["b"])

        # Model constructor called only once
        mock_cls.assert_called_once()


class TestEmbedQuery:
    def setup_method(self):
        embeddings_mod._models.clear()

    def test_uses_query_prompt(self):
        model = _mock_model()
        model.encode.return_value = np.zeros(768, dtype=np.float32)

        with patch("sentence_transformers.SentenceTransformer", return_value=model):
            embed_query("search term")

        model.encode.assert_called_once_with(
            "search term",
            prompt_name="query",
            normalize_embeddings=True,
            show_progress_bar=False,
        )
