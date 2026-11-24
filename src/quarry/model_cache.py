"""Resolve the cache directory for Quarry's Hugging Face model files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, final

from quarry.home_paths import QuarryHome

ONNX_MODEL_REPO = "Snowflake/snowflake-arctic-embed-m-v1.5"
ONNX_MODEL_REVISION = "e58a8f756156a1293d763f17e3aae643474e9b8a"
ONNX_TOKENIZER_FILE = "tokenizer.json"
ONNX_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class _CacheLookup(Protocol):
    def __call__(
        self, repo_id: str, filename: str, *, revision: str, cache_dir: Path
    ) -> object: ...


@final
class ModelCache:
    """The FS-4 cache location shared by downloads and cached reads."""

    __slots__ = ()

    @staticmethod
    def override() -> Path | None:
        """Return an explicit override in Hugging Face precedence order."""
        if hub := os.environ.get("HF_HUB_CACHE") or os.environ.get(
            "HUGGINGFACE_HUB_CACHE"
        ):
            return Path(os.path.expandvars(hub))
        if home := os.environ.get("HF_HOME"):
            return Path(os.path.expandvars(home)) / "hub"
        if xdg := os.environ.get("XDG_CACHE_HOME"):
            return Path(os.path.expandvars(xdg)) / "huggingface" / "hub"
        return None

    @classmethod
    def root(cls) -> Path:
        """Resolve the selected cache without falling back into the checkout."""
        if (override := cls.override()) is None:
            return QuarryHome.cache_root()
        expanded = QuarryHome.expanded(override)
        return expanded if expanded.is_absolute() else QuarryHome.home() / expanded

    @classmethod
    def requires_home(cls) -> bool:
        """Whether resolving the selected cache requires the user's home."""
        override = cls.override()
        return override is None or not override.is_absolute()

    @classmethod
    def cached_size(
        cls, lookup: _CacheLookup, *, cache_dir: Path | None = None
    ) -> int | None:
        """Return the cached INT8 model size only when both files are present."""
        cache = cache_dir if cache_dir is not None else cls.root()
        paths: list[Path] = []
        for filename in ("onnx/model_int8.onnx", ONNX_TOKENIZER_FILE):
            hit = lookup(
                ONNX_MODEL_REPO, filename, revision=ONNX_MODEL_REVISION, cache_dir=cache
            )
            if not isinstance(hit, str) or not (path := Path(hit)).exists():
                return None
            paths.append(path)
        return paths[0].stat().st_size

    @classmethod
    def fetch(cls, model_file: str, *, local_files_only: bool) -> tuple[str, str]:
        """Get both ONNX artifacts from the selected cache."""
        from huggingface_hub import hf_hub_download  # noqa: PLC0415

        cache = cls.root()

        def download(filename: str) -> str:
            return hf_hub_download(
                repo_id=ONNX_MODEL_REPO,
                filename=filename,
                revision=ONNX_MODEL_REVISION,
                cache_dir=cache,
                local_files_only=local_files_only,
            )

        return download(model_file), download(ONNX_TOKENIZER_FILE)
