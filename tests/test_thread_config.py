"""Tests for ThreadConfig — ONNX/OMP thread budget derivation (DES-032).

Verifies the hardware/provider-to-thread-count mapping and the environment-cap
side effects directly, without constructing the full ONNX backend.
"""

from __future__ import annotations

import logging
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

    def test_unknown_cpu_count_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The None->4 fallback must not be silent: an operator needs to know the
        # thread budget was guessed, not measured.
        monkeypatch.setattr("os.cpu_count", lambda: None)
        with caplog.at_level(logging.WARNING, logger="quarry.thread_config"):
            ThreadConfig(is_gpu=False)
        assert any(
            "os.cpu_count() returned None" in rec.getMessage() for rec in caplog.records
        )

    def test_known_cpu_count_does_not_warn(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        with caplog.at_level(logging.WARNING, logger="quarry.thread_config"):
            ThreadConfig(is_gpu=False)
        assert not any("returned None" in rec.getMessage() for rec in caplog.records)


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

    def test_divergent_preset_warns_mitigation_defeated(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A preset above the cap means the oversubscription fix is not in force;
        # the logs must say so rather than claim the intended budget is active.
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        monkeypatch.setenv("OMP_NUM_THREADS", "7")
        with caplog.at_level(logging.WARNING, logger="quarry.thread_config"):
            ThreadConfig(is_gpu=False).apply_env_limits()
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("may be defeated" in m for m in warnings)
        assert any("preset to 7" in m for m in warnings)

    def test_logs_effective_not_intended_omp(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The info line must report the value actually in the environment (7),
        # not the value ThreadConfig wanted to set (2).
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        monkeypatch.setenv("OMP_NUM_THREADS", "7")
        with caplog.at_level(logging.INFO, logger="quarry.thread_config"):
            ThreadConfig(is_gpu=False).apply_env_limits()
        rendered = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any("OMP=7" in line for line in rendered)
        assert not any("OMP=2" in line for line in rendered)

    def test_no_warning_when_cap_applied(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
        with caplog.at_level(logging.WARNING, logger="quarry.thread_config"):
            ThreadConfig(is_gpu=False).apply_env_limits()
        assert not any("may be defeated" in r.getMessage() for r in caplog.records)

    def test_apply_returns_self(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("os.cpu_count", lambda: 8)
        config = ThreadConfig(is_gpu=False)
        assert config.apply_env_limits() is config
