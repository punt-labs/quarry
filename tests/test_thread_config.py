"""Tests for ThreadConfig — ONNX/OMP thread budget derivation (DES-032).

Verifies the hardware/provider-to-thread-count mapping and the environment-cap
side effects directly, without constructing the full ONNX backend.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from quarry.thread_config import ThreadConfig

if TYPE_CHECKING:
    import pytest


class TestIntraOpThreads:
    def test_gpu_uses_one_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # GPU offloads GEMMs to CUDA; one CPU feeder thread suffices.
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        assert ThreadConfig(is_gpu=True).intra_op_threads == 1

    def test_cpu_caps_at_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        assert ThreadConfig(is_gpu=False).intra_op_threads == 2

    def test_cpu_below_cap_uses_ncpu(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("os.cpu_count", lambda: 1)
        assert ThreadConfig(is_gpu=False).intra_op_threads == 1

    def test_unknown_cpu_count_falls_back_to_four(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # os.cpu_count() can return None; ThreadConfig must not crash.
        monkeypatch.setattr("os.cpu_count", lambda: None)
        assert ThreadConfig(is_gpu=False).intra_op_threads == 2


class TestApplyEnvLimits:
    def test_sets_tokenizers_parallelism_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        monkeypatch.delenv("TOKENIZERS_PARALLELISM", raising=False)
        ThreadConfig(is_gpu=False).apply_env_limits()
        assert os.environ.get("TOKENIZERS_PARALLELISM") == "false"

    def test_sets_omp_to_min_two_ncpu(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
        ThreadConfig(is_gpu=False).apply_env_limits()
        assert os.environ.get("OMP_NUM_THREADS") == "2"

    def test_omp_is_one_on_single_core(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("os.cpu_count", lambda: 1)
        monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
        ThreadConfig(is_gpu=False).apply_env_limits()
        assert os.environ.get("OMP_NUM_THREADS") == "1"

    def test_preserves_operator_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # setdefault must not clobber an explicit operator setting.
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        monkeypatch.setenv("OMP_NUM_THREADS", "7")
        ThreadConfig(is_gpu=False).apply_env_limits()
        assert os.environ.get("OMP_NUM_THREADS") == "7"

    def test_apply_returns_self(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        config = ThreadConfig(is_gpu=False)
        assert config.apply_env_limits() is config
