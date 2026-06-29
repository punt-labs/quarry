"""Tests for OnnxSessionBuilder — ONNX GPU-to-CPU fallback (DES-016/032).

Covers the happy path plus every branch of the CUDA-failure policy: eligible
fallback, forced-CUDA re-raise, non-CUDA-error re-raise, and double failure.
``onnxruntime`` is patched via a fake module so no real model is loaded.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Self
from unittest.mock import MagicMock, patch

import pytest

from quarry.ingestion.provider import ProviderSelection
from quarry.onnx_session import OnnxSessionBuilder
from quarry.thread_config import ThreadConfig

_CUDA = ProviderSelection(provider="CUDAExecutionProvider", model_file="fp16.onnx")
_CPU = ProviderSelection(provider="CPUExecutionProvider", model_file="int8.onnx")


class _RecordingOptions:
    """A SessionOptions stand-in that records the thread/graph knobs set on it.

    ``ThreadConfig.apply_to_session`` assigns ``intra_op_num_threads`` and
    ``inter_op_num_threads``; the builder sets ``graph_optimization_level``.
    Capturing them lets a test assert the CPU fallback used the CPU thread
    budget rather than reusing the GPU session's ``intra_op=1``.
    """

    intra_op_num_threads: int
    inter_op_num_threads: int
    graph_optimization_level: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0
        self.graph_optimization_level = 0
        return self


def _load_cpu(_model_file: str) -> str:
    return "/fake/cpu_model.onnx"


def _builder(
    selection: ProviderSelection, *, force_cuda: bool = False
) -> OnnxSessionBuilder:
    return OnnxSessionBuilder(
        selection,
        ThreadConfig(is_gpu=selection.provider == "CUDAExecutionProvider"),
        force_cuda=force_cuda,
        load_cpu_model=_load_cpu,
    )


def _fake_ort(inference_session: MagicMock) -> SimpleNamespace:
    """A stand-in onnxruntime module exposing the symbols the builder uses."""
    return SimpleNamespace(
        SessionOptions=lambda: MagicMock(name="SessionOptions"),
        GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL=99),
        InferenceSession=inference_session,
    )


class TestBuild:
    def test_returns_session_on_success(self) -> None:
        sentinel = MagicMock(name="session")
        fake = _fake_ort(MagicMock(return_value=sentinel))
        with patch.dict("sys.modules", {"onnxruntime": fake}):
            result = _builder(_CPU).build("/m.onnx")
        assert result is sentinel
        assert fake.InferenceSession.call_count == 1

    def test_cuda_failure_falls_back_to_cpu(self) -> None:
        cpu_session = MagicMock(name="cpu_session")
        calls = 0

        def side_effect(*_args: object, **_kwargs: object) -> MagicMock:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("cuDNN not found")
            return cpu_session

        fake = _fake_ort(MagicMock(side_effect=side_effect))
        with patch.dict("sys.modules", {"onnxruntime": fake}):
            result = _builder(_CUDA).build("/m.onnx")
        assert result is cpu_session
        assert calls == 2

    def test_cpu_fallback_uses_cpu_thread_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # After a GPU->CPU fallback the CPU session must run at the CPU intra-op
        # budget (min(2, ncpu)), NOT the GPU session's intra_op=1 (DES-032).
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        cpu_session = MagicMock(name="cpu_session")
        captured: dict[str, _RecordingOptions] = {}
        calls = 0

        def side_effect(
            _model_path: str, *, sess_options: _RecordingOptions, providers: list[str]
        ) -> MagicMock:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("cuDNN not found")
            captured["cpu"] = sess_options
            return cpu_session

        fake = SimpleNamespace(
            SessionOptions=_RecordingOptions,
            GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL=99),
            InferenceSession=MagicMock(side_effect=side_effect),
        )
        with patch.dict("sys.modules", {"onnxruntime": fake}):
            result = _builder(_CUDA).build("/m.onnx")
        assert result is cpu_session
        assert captured["cpu"].intra_op_num_threads == min(2, 8)
        assert captured["cpu"].intra_op_num_threads != 1

    def test_forced_cuda_reraises(self) -> None:
        # When the operator pins CUDA, a CUDA failure must propagate, not fall back.
        fake = _fake_ort(MagicMock(side_effect=RuntimeError("cuDNN not found")))
        with (
            patch.dict("sys.modules", {"onnxruntime": fake}),
            pytest.raises(RuntimeError, match="cuDNN"),
        ):
            _builder(_CUDA, force_cuda=True).build("/m.onnx")

    def test_non_cuda_error_reraises(self) -> None:
        # A failure unrelated to CUDA is a real bug — do not mask it with fallback.
        fake = _fake_ort(MagicMock(side_effect=RuntimeError("corrupt model file")))
        with (
            patch.dict("sys.modules", {"onnxruntime": fake}),
            pytest.raises(RuntimeError, match="corrupt model"),
        ):
            _builder(_CUDA).build("/m.onnx")

    def test_cpu_provider_error_reraises(self) -> None:
        # CPU was the selected provider; there is no further fallback.
        fake = _fake_ort(MagicMock(side_effect=RuntimeError("cuda oom")))
        with (
            patch.dict("sys.modules", {"onnxruntime": fake}),
            pytest.raises(RuntimeError, match="cuda oom"),
        ):
            _builder(_CPU).build("/m.onnx")

    def test_double_failure_raises_runtimeerror(self) -> None:
        # CUDA fails, then the CPU fallback also fails -> single RuntimeError.
        fake = _fake_ort(MagicMock(side_effect=RuntimeError("cuda init failed")))
        with (
            patch.dict("sys.modules", {"onnxruntime": fake}),
            pytest.raises(RuntimeError, match="CPU fallback also failed"),
        ):
            _builder(_CUDA).build("/m.onnx")
