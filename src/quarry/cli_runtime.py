"""Runtime helpers shared by CLI commands: worker sizing and failure exits."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

import typer

if TYPE_CHECKING:
    from quarry.config import Settings


@final
class CliRuntime:
    """Command-runtime decisions that several CLI commands share."""

    __slots__ = ()

    @staticmethod
    def auto_workers(settings: Settings) -> int:  # noqa: ARG004
        """Return 4 for CUDA (GPU), 1 for CPU; fall back to 1 on probe failure.

        The fallback covers the expected failure modes of provider detection: a
        missing optional dependency (``ImportError`` from the lazy import or
        onnxruntime) and a hardware/provider probe that fails (``OSError`` or
        ``RuntimeError``).  A misconfigured ``QUARRY_PROVIDER`` (``ValueError``)
        and any genuinely unexpected error surface rather than being masked by a
        silent single-worker default.
        """
        try:
            from quarry.ingestion.provider import ProviderSelection  # noqa: PLC0415

            prov = ProviderSelection.from_environment().provider
        except (ImportError, OSError, RuntimeError):
            return 1
        return 4 if prov == "CUDAExecutionProvider" else 1

    @staticmethod
    def exit_on_ingest_failure(result: dict[str, object] | object) -> None:
        """Exit 1 if *result* reports errors and zero ingested chunks.

        Both the local pipeline and the remote HTTP response use the same
        ``{errors, chunks}`` shape.  A successful operation may report errors
        alongside a positive chunk count (partial success); only the
        all-or-nothing failure case is promoted to a non-zero exit code.
        """
        if not isinstance(result, dict):
            return
        errors_raw = result.get("errors")
        if not isinstance(errors_raw, list) or not errors_raw:
            return
        chunks_raw = result.get("chunks", 0)
        try:
            chunks = int(chunks_raw) if isinstance(chunks_raw, int | float | str) else 0
        except (TypeError, ValueError):
            chunks = 0
        if chunks == 0:
            raise typer.Exit(code=1)
