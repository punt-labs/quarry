"""The embedding-model doctor check: is the model cached, and where does it live."""

from __future__ import annotations

from typing import final

from quarry.doctor_failure import CheckFailure
from quarry.doctor_preconditions import Need
from quarry.model_cache import ModelCache
from quarry.results import CheckResult
from quarry.service_environment import ServiceEnvironment


@final
class EmbeddingModel:
    """Doctor's view of the embedding model's cache."""

    __slots__ = ()

    @staticmethod
    def needs() -> Need:
        """Return whether the cache depends on an available home directory."""
        return Need.HOME if ModelCache.requires_home() else Need.NONE

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
            model_size = ModelCache.cached_size(
                try_to_load_from_cache, cache_dir=ServiceEnvironment.installed_cache()
            )
        except CheckFailure.BROKEN_STACK as exc:
            return CheckResult(
                name="Embedding model",
                passed=False,
                message=f"cannot read the model cache ({CheckFailure.detail(exc)})",
            )
        message = (
            "Not cached (run 'quarry install')"
            if model_size is None
            else "snowflake-arctic-embed-m-v1.5 (ONNX INT8) cached"
            f" ({EmbeddingModel.human_size(model_size)})"
        )
        return CheckResult(
            name="Embedding model", passed=model_size is not None, message=message
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
