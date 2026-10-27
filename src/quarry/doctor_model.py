"""The embedding-model doctor check: is the model cached, and where does it live."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import final

from quarry.config import ONNX_MODEL_REPO, ONNX_MODEL_REVISION, ONNX_TOKENIZER_FILE
from quarry.doctor_failure import CheckFailure
from quarry.doctor_preconditions import Need
from quarry.results import CheckResult


@final
class EmbeddingModel:
    """Doctor's view of the embedding model's cache."""

    __slots__ = ()

    @staticmethod
    def needs() -> Need:
        """Return what locating the cache needs, as huggingface_hub resolves it.

        The cache root is settled once the environment is applied (``HF_HOME``,
        ``HF_HUB_CACHE``, ``HUGGINGFACE_HUB_CACHE``, ``XDG_CACHE_HOME``); it is
        absolute when one of those names it, and otherwise it is a path under the
        home directory that did not resolve.
        """
        try:
            constants = importlib.import_module("huggingface_hub.constants")
        except CheckFailure.BROKEN_STACK:
            return Need.HOME  # the check itself reports the broken import
        return Need.NONE if Path(constants.HF_HUB_CACHE).is_absolute() else Need.HOME

    @staticmethod
    def check() -> CheckResult:
        """Report whether the embedding model is in the local cache."""
        # Imported here, inside a try: a broken install is a failed check, not an
        # import error that takes the whole report down.
        try:
            from huggingface_hub import try_to_load_from_cache  # noqa: PLC0415
        except CheckFailure.BROKEN_STACK as exc:
            return CheckResult(
                name="Embedding model",
                passed=False,
                message=CheckFailure.import_message("huggingface_hub", exc),
            )
        try:
            model_cached = try_to_load_from_cache(
                ONNX_MODEL_REPO,
                "onnx/model_int8.onnx",
                revision=ONNX_MODEL_REVISION,
            )
            tokenizer_cached = try_to_load_from_cache(
                ONNX_MODEL_REPO, ONNX_TOKENIZER_FILE, revision=ONNX_MODEL_REVISION
            )
            if (
                isinstance(model_cached, str)
                and Path(model_cached).exists()
                and isinstance(tokenizer_cached, str)
                and Path(tokenizer_cached).exists()
            ):
                model_size = Path(model_cached).stat().st_size
                return CheckResult(
                    name="Embedding model",
                    passed=True,
                    message=(
                        "snowflake-arctic-embed-m-v1.5 (ONNX INT8) cached"
                        f" ({EmbeddingModel.human_size(model_size)})"
                    ),
                )
        except CheckFailure.BROKEN_STACK as exc:
            return CheckResult(
                name="Embedding model",
                passed=False,
                message=f"cannot read the model cache ({CheckFailure.detail(exc)})",
            )
        return CheckResult(
            name="Embedding model",
            passed=False,
            message="Not cached (run 'quarry install')",
        )

    @staticmethod
    def human_size(nbytes: float) -> str:
        """Format byte count as human-readable string (float: /=1024 stays in-type)."""
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if nbytes < 1024 or unit == "TB":
                places = 1 if nbytes >= 10 else 2
                return f"{nbytes:.{places}f} {unit}"
            nbytes /= 1024
        return f"{nbytes:.1f} TB"  # unreachable but satisfies type checker
