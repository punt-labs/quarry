from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ocr.types import EmbeddingModel

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)

_model: EmbeddingModel | None = None


def _get_model(model_name: str) -> EmbeddingModel:
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        logger.info("Loading embedding model: %s", model_name)
        _model = SentenceTransformer(model_name)
        logger.info("Model loaded")
    return _model


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
    """
    model = _get_model(model_name)
    return model.encode(
        query,
        prompt_name="query",
        normalize_embeddings=True,
        show_progress_bar=False,
    )
