from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

import numpy as np

from quarry.config import (
    ONNX_MODEL_FILE,
    ONNX_MODEL_REPO,
    ONNX_QUERY_PREFIX,
    ONNX_TOKENIZER_FILE,
)

if TYPE_CHECKING:
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)

_backends: dict[str, OnnxEmbeddingBackend] = {}
_lock = threading.Lock()


def _download_model_files() -> tuple[str, str]:
    """Download ONNX model and tokenizer via huggingface-hub.

    Returns:
        Tuple of (model_path, tokenizer_path) as absolute file paths.
    """
    from huggingface_hub import hf_hub_download  # noqa: PLC0415

    model_path = hf_hub_download(repo_id=ONNX_MODEL_REPO, filename=ONNX_MODEL_FILE)
    tokenizer_path = hf_hub_download(
        repo_id=ONNX_MODEL_REPO, filename=ONNX_TOKENIZER_FILE
    )
    return model_path, tokenizer_path


def _get_backend(model_name: str) -> OnnxEmbeddingBackend:
    """Thread-safe cached backend lookup."""
    if model_name not in _backends:
        with _lock:
            if model_name not in _backends:
                _backends[model_name] = OnnxEmbeddingBackend(model_name)
    return _backends[model_name]


class OnnxEmbeddingBackend:
    """Embedding backend using ONNX Runtime directly.

    Replaces sentence-transformers to eliminate the torch dependency (~2.5 GB).
    Uses the INT8 quantized ONNX export of snowflake-arctic-embed-m-v1.5.
    """

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._dimension = 768

        model_path, tokenizer_path = _download_model_files()

        from tokenizers import Tokenizer  # noqa: PLC0415

        logger.info("Loading ONNX embedding model: %s", model_name)
        self._tokenizer = Tokenizer.from_file(tokenizer_path)
        self._tokenizer.enable_padding()
        self._tokenizer.enable_truncation(max_length=512)

        import onnxruntime as ort  # noqa: PLC0415

        self._session = ort.InferenceSession(model_path)
        logger.info("ONNX embedding model loaded")

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        """Embed a batch of texts. Returns shape (n, dimension)."""
        encodings = self._tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        token_type_ids = np.zeros_like(input_ids)

        (hidden_states,) = self._session.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            },
        )

        # CLS pooling: take the first token's hidden state
        cls_embeddings: NDArray[np.float32] = hidden_states[:, 0, :]

        # L2 normalize
        norms = np.linalg.norm(cls_embeddings, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-12)
        normalized: NDArray[np.float32] = cls_embeddings / norms
        return normalized

    def embed_query(self, query: str) -> NDArray[np.float32]:
        """Embed a search query. Returns shape (dimension,)."""
        prefixed = ONNX_QUERY_PREFIX + query
        result: NDArray[np.float32] = self.embed_texts([prefixed])[0]
        return result


# Public API — used by pipeline.py and mcp_server.py via backends.py


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
        OSError: If the embedding model cannot be loaded.
    """
    return _get_backend(model_name).embed_texts(texts)


def embed_query(
    query: str,
    model_name: str = "Snowflake/snowflake-arctic-embed-m-v1.5",
) -> NDArray[np.float32]:
    """Generate embedding for a search query.

    Args:
        query: Search query text.
        model_name: HuggingFace model identifier.

    Returns:
        Array of shape (768,) with normalized embedding.

    Raises:
        OSError: If the embedding model cannot be loaded.
    """
    return _get_backend(model_name).embed_query(query)
