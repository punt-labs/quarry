"""The ONNX execution-provider health check."""

from __future__ import annotations

from typing import final

from quarry.doctor_failure import CheckFailure
from quarry.ingestion.provider import ProviderSelection
from quarry.results import CheckResult


@final
class InferenceDiagnostics:
    """``quarry doctor``'s report of the selected ONNX execution provider.

    Advisory (``required=False``): quarry's core runs on the CPU provider, so a
    machine without a GPU provider warns rather than failing ``quarry install``.
    Reading the selection is a configuration lookup and costs nothing; it does
    not construct a session or load a model.
    """

    __slots__ = ()

    @staticmethod
    def onnx_provider() -> CheckResult:
        """Report which ONNX execution provider is selected."""
        try:
            selection = ProviderSelection.from_environment()
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                name="ONNX provider",
                passed=False,
                message=f"cannot select a provider ({CheckFailure.detail(exc)})",
                required=False,
            )
        return CheckResult(
            name="ONNX provider",
            passed=True,
            message=f"{selection.provider} ({selection.model_file})",
            required=False,
        )
