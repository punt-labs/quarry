"""ONNX-based text embedding for semantic search."""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Self

import numpy as np

from quarry.config import (
    ONNX_MODEL_REPO,
    ONNX_MODEL_REVISION,
    ONNX_QUERY_PREFIX,
    ONNX_TOKENIZER_FILE,
)
from quarry.ingestion.provider import PROVIDER_MODEL_MAP, ProviderSelection
from quarry.onnx_session import OnnxSessionBuilder
from quarry.thread_config import ThreadConfig

if TYPE_CHECKING:
    import onnxruntime as ort
    from numpy.typing import NDArray
    from tokenizers import Tokenizer

logger = logging.getLogger(__name__)

_EMBED_BATCH_SIZE: int = 32


class OnnxEmbeddingBackend:
    """Embedding backend using ONNX Runtime directly.

    Replaces sentence-transformers to eliminate the torch dependency (~2.5 GB).
    Uses the INT8 quantized ONNX export of snowflake-arctic-embed-m-v1.5.
    """

    _dimension: int
    _tokenizer: Tokenizer
    _session: ort.InferenceSession

    @classmethod
    def download_model_files(
        cls, model_file: str = "onnx/model_int8.onnx"
    ) -> tuple[str, str]:
        """Download ONNX model and tokenizer from HuggingFace Hub."""
        from huggingface_hub import hf_hub_download  # noqa: PLC0415

        model_path = hf_hub_download(
            repo_id=ONNX_MODEL_REPO,
            filename=model_file,
            revision=ONNX_MODEL_REVISION,
        )
        tokenizer_path = hf_hub_download(
            repo_id=ONNX_MODEL_REPO,
            filename=ONNX_TOKENIZER_FILE,
            revision=ONNX_MODEL_REVISION,
        )
        return model_path, tokenizer_path

    @classmethod
    def _load_model_files(cls, model_file: str) -> tuple[str, str]:
        """Load ONNX model and tokenizer, downloading if not cached."""
        try:
            return cls._load_local_model_files(model_file)
        except OSError:
            logger.info("Embedding model not cached -- downloading (~120-220 MB)")
            return cls.download_model_files(model_file)

    @classmethod
    def _load_local_model_files(cls, model_file: str) -> tuple[str, str]:
        """Load ONNX model and tokenizer from local cache only."""
        from huggingface_hub import hf_hub_download  # noqa: PLC0415

        model_path = hf_hub_download(
            repo_id=ONNX_MODEL_REPO,
            filename=model_file,
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

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._dimension = 768

        selection = ProviderSelection.from_environment()
        threads = ThreadConfig.for_provider(selection.provider).apply_env_limits()

        force_cuda = os.environ.get("QUARRY_PROVIDER", "").strip().lower() == "cuda"

        if selection.provider == "CUDAExecutionProvider":
            try:
                model_path, tokenizer_path = cls._load_model_files(selection.model_file)
            except Exception as load_exc:
                if not force_cuda:
                    logger.warning(
                        "FP16 model load failed, falling back to CPU + int8: %s",
                        load_exc,
                    )
                    cpu_model_file = PROVIDER_MODEL_MAP["CPUExecutionProvider"]
                    model_path, tokenizer_path = cls._load_model_files(cpu_model_file)
                    selection = ProviderSelection(
                        provider="CPUExecutionProvider",
                        model_file=cpu_model_file,
                    )
                else:
                    raise
        else:
            model_path, tokenizer_path = cls._load_model_files(selection.model_file)

        from tokenizers import Tokenizer  # noqa: PLC0415

        logger.info("Loading ONNX embedding model: %s", ONNX_MODEL_REPO)
        self._tokenizer = Tokenizer.from_file(tokenizer_path)
        self._tokenizer.enable_padding()
        self._tokenizer.enable_truncation(max_length=512)

        builder = OnnxSessionBuilder(
            selection,
            threads,
            force_cuda=force_cuda,
            load_cpu_model=lambda model_file: cls._load_model_files(model_file)[0],
        )
        self._session = builder.build(model_path)
        return self

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return ONNX_MODEL_REPO

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        """Embed a batch of texts. Returns shape (n, dimension).

        Processes texts in batches of ``_EMBED_BATCH_SIZE`` to bound peak
        memory.  Without batching, a 575-text call allocates ~15 GB per
        attention layer — enough to OOM-kill a 24 GB laptop.
        """
        if not texts:
            return np.empty((0, self._dimension), dtype=np.float32)

        n = len(texts)
        n_batches = (n + _EMBED_BATCH_SIZE - 1) // _EMBED_BATCH_SIZE
        logger.debug("Embedding %d texts in %d batches", n, n_batches)

        if n > 256:
            logger.warning(
                "Large embedding request (%d texts, %d batches). "
                "This document is unusually large; embedding will be slow.",
                n,
                n_batches,
            )

        t_total_start = time.perf_counter()
        parts: list[NDArray[np.float32]] = []
        for i in range(n_batches):
            batch = texts[i * _EMBED_BATCH_SIZE : (i + 1) * _EMBED_BATCH_SIZE]
            t_batch_start = time.perf_counter()
            encodings = self._tokenizer.encode_batch(batch)
            input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
            attention_mask = np.array(
                [e.attention_mask for e in encodings], dtype=np.int64
            )
            _token_embeddings, sentence_embedding = self._session.run(
                None,
                {
                    "input_ids": input_ids,
                    "attention_mask": attention_mask,
                },
            )
            t_batch_elapsed = time.perf_counter() - t_batch_start
            parts.append(np.asarray(sentence_embedding, dtype=np.float32))
            logger.debug(
                "embedding: batch %d/%d (%d texts) in %.2fs",
                i + 1,
                n_batches,
                len(batch),
                t_batch_elapsed,
            )

        t_total_elapsed = time.perf_counter() - t_total_start
        logger.info(
            "embedding: %d texts in %.2fs (%.1f texts/s)",
            n,
            t_total_elapsed,
            n / t_total_elapsed if t_total_elapsed > 0 else float("inf"),
        )

        result: NDArray[np.float32] = np.concatenate(parts)
        # L2-normalize each row to unit length so that cosine similarity equals
        # the dot product of stored and query vectors (quarry-3a7f). The
        # zero-norm guard divides by 1.0 instead of 0.0 so a pathological
        # all-zero embedding stays all-zero rather than becoming NaN.
        norms = np.linalg.norm(result, axis=1, keepdims=True)
        norms = np.where(norms == 0.0, np.float32(1.0), norms)
        normalized: NDArray[np.float32] = (result / norms).astype(np.float32)
        return normalized

    def embed_query(self, query: str) -> NDArray[np.float32]:
        """Embed a search query. Returns shape (dimension,)."""
        prefixed = ONNX_QUERY_PREFIX + query
        result: NDArray[np.float32] = self.embed_texts([prefixed])[0]
        return result
