"""Tests for quarry.provider -- ONNX Runtime provider auto-detection."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from quarry.provider import ProviderSelection, select_provider


class TestSelectProvider:
    def test_cpu_only_returns_cpu_int8(self) -> None:
        with patch(
            "onnxruntime.get_available_providers",
            return_value=["CPUExecutionProvider"],
        ):
            result = select_provider()

        assert result == ProviderSelection(
            "CPUExecutionProvider", "onnx/model_int8.onnx"
        )

    def test_cuda_available_returns_cuda_fp16(self) -> None:
        with patch(
            "onnxruntime.get_available_providers",
            return_value=["CUDAExecutionProvider", "CPUExecutionProvider"],
        ):
            result = select_provider()

        assert result == ProviderSelection(
            "CUDAExecutionProvider", "onnx/model_fp16.onnx"
        )

    def test_env_cpu_overrides_cuda(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QUARRY_PROVIDER", "cpu")
        with patch(
            "onnxruntime.get_available_providers",
            return_value=["CUDAExecutionProvider", "CPUExecutionProvider"],
        ):
            result = select_provider()

        assert result == ProviderSelection(
            "CPUExecutionProvider", "onnx/model_int8.onnx"
        )

    def test_env_cuda_with_cuda_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QUARRY_PROVIDER", "cuda")
        with patch(
            "onnxruntime.get_available_providers",
            return_value=["CUDAExecutionProvider", "CPUExecutionProvider"],
        ):
            result = select_provider()

        assert result == ProviderSelection(
            "CUDAExecutionProvider", "onnx/model_fp16.onnx"
        )

    def test_env_cuda_without_cuda_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QUARRY_PROVIDER", "cuda")
        with (
            patch(
                "onnxruntime.get_available_providers",
                return_value=["CPUExecutionProvider"],
            ),
            pytest.raises(RuntimeError, match="CUDAExecutionProvider not available"),
        ):
            select_provider()

    def test_env_unknown_value_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QUARRY_PROVIDER", "rocm")
        with pytest.raises(ValueError, match="Unknown QUARRY_PROVIDER"):
            select_provider()

    def test_cpu_override_logs_info(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("QUARRY_PROVIDER", "cpu")
        with (
            patch(
                "onnxruntime.get_available_providers",
                return_value=["CPUExecutionProvider"],
            ),
            caplog.at_level(logging.INFO, logger="quarry.provider"),
        ):
            select_provider()

        assert "Provider override: cpu (QUARRY_PROVIDER)" in caplog.text

    def test_empty_providers_returns_cpu(self) -> None:
        with patch(
            "onnxruntime.get_available_providers",
            return_value=[],
        ):
            result = select_provider()

        assert result == ProviderSelection(
            "CPUExecutionProvider", "onnx/model_int8.onnx"
        )
