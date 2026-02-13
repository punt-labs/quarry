"""ONNX-based text embedding for semantic search."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

from quarry.config import (
    ONNX_MODEL_FILE,
    ONNX_MODEL_REPO,
    ONNX_MODEL_REVISION,
    ONNX_QUERY_PREFIX,
    ONNX_TOKENIZER_FILE,
)

if TYPE_CHECKING:
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)


def _download_model_files() -> tuple[str, str]:
    """Download ONNX model and tokenizer from HuggingFace Hub.

    Makes network requests. Used by ``quarry install`` only.

    Returns:
        Tuple of (model_path, tokenizer_path) as absolute file paths.
    """
    from huggingface_hub import hf_hub_download  # noqa: PLC0415

    model_path = hf_hub_download(
        repo_id=ONNX_MODEL_REPO,
        filename=ONNX_MODEL_FILE,
        revision=ONNX_MODEL_REVISION,
    )
    tokenizer_path = hf_hub_download(
        repo_id=ONNX_MODEL_REPO,
        filename=ONNX_TOKENIZER_FILE,
        revision=ONNX_MODEL_REVISION,
    )
    return model_path, tokenizer_path


def _load_model_files() -> tuple[str, str]:
    """Load ONNX model and tokenizer from local cache.

    No network requests. Raises if files are not cached.

    Returns:
        Tuple of (model_path, tokenizer_path) as absolute file paths.

    Raises:
        OSError: If the model files are not in the local cache.
    """
    from huggingface_hub import hf_hub_download  # noqa: PLC0415

    model_path = hf_hub_download(
        repo_id=ONNX_MODEL_REPO,
        filename=ONNX_MODEL_FILE,
        revision=ONNX_MODEL_REVISION,
        local_files_only=True,
    )
    tokenizer_path = hf_hub_download(
        repo_id=ONNX_MODEL_REPO,
        filename=ONNX_TOKENIZER_FILE,
        revision=ONNX_MODEL_REVISION,
        local_files_only=True,
    )
    return model_path, tokenizer_path


class OnnxEmbeddingBackend:
    """Embedding backend using ONNX Runtime directly.

    Replaces sentence-transformers to eliminate the torch dependency (~2.5 GB).
    Uses the INT8 quantized ONNX export of snowflake-arctic-embed-m-v1.5.
    """

    def __init__(self) -> None:
        self._dimension = 768

        model_path, tokenizer_path = _load_model_files()

        from tokenizers import Tokenizer  # noqa: PLC0415

        logger.info("Loading ONNX embedding model: %s", ONNX_MODEL_REPO)
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
        return ONNX_MODEL_REPO

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        """Embed a batch of texts. Returns shape (n, dimension)."""
        if not texts:
            return np.empty((0, self._dimension), dtype=np.float32)
        encodings = self._tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)

        _token_embeddings, sentence_embedding = self._session.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
            },
        )

        # Model provides pre-pooled, pre-normalized sentence embeddings
        result: NDArray[np.float32] = sentence_embedding
        return result

    def embed_query(self, query: str) -> NDArray[np.float32]:
        """Embed a search query. Returns shape (dimension,)."""
        prefixed = ONNX_QUERY_PREFIX + query
        result: NDArray[np.float32] = self.embed_texts([prefixed])[0]
        return result
