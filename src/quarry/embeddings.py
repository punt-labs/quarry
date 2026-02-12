from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from quarry.config import EMBEDDING_MODEL_REVISION
from quarry.types import EmbeddingModel

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from quarry.config import Settings

logger = logging.getLogger(__name__)

_models: dict[str, EmbeddingModel] = {}


def _get_model(model_name: str) -> EmbeddingModel:
    if model_name not in _models:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        logger.info("Loading embedding model: %s (offline)", model_name)
        _models[model_name] = SentenceTransformer(
            model_name,
            revision=EMBEDDING_MODEL_REVISION,
            local_files_only=True,
        )
        logger.info("Model loaded")
    return _models[model_name]


def embed_texts(
    texts: list[str],
    model_name: str = "Snowflake/snowflake-arctic-embed-m-v1.5",
) -> NDArray[np.float32]:
    """Generate embeddings for a list of texts.

    Args:
        texts: Texts to embed.
        model_name: HuggingFace model identifier.

    Returns:
        Array of shape (len(texts), 768) with normalized embeddings.

    Raises:
        OSError: If the embedding model cannot be loaded or downloaded.
    """
    model = _get_model(model_name)
    return model.encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=False,
    )


def embed_query(
    query: str,
    model_name: str = "Snowflake/snowflake-arctic-embed-m-v1.5",
) -> NDArray[np.float32]:
    """Generate embedding for a search query.

    snowflake-arctic-embed-m-v1.5 uses query prefixes automatically
    via the sentence-transformers prompt handling.

    Args:
        query: Search query text.
        model_name: HuggingFace model identifier.

    Returns:
        Array of shape (768,) with normalized embedding.

    Raises:
        OSError: If the embedding model cannot be loaded or downloaded.
    """
    model = _get_model(model_name)
    return model.encode(
        query,
        prompt_name="query",
        normalize_embeddings=True,
        show_progress_bar=False,
    )


class SnowflakeEmbeddingBackend:
    """Embedding backend using sentence-transformers.

    Satisfies the ``EmbeddingBackend`` protocol.  Delegates to the
    module-level ``_get_model()`` cache so the model is shared with
    the free functions during the migration period.
    """

    def __init__(self, settings: Settings) -> None:
        self._model_name = settings.embedding_model
        self._dimension = settings.embedding_dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        """Embed a batch of texts. Returns shape (n, dimension)."""
        return embed_texts(texts, model_name=self._model_name)

    def embed_query(self, query: str) -> NDArray[np.float32]:
        """Embed a search query. Returns shape (dimension,)."""
        return embed_query(query, model_name=self._model_name)
