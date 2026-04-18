from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.logging_config import configure_logging

if TYPE_CHECKING:
    import pytest


def test_env_var_overrides_stderr_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """QUARRY_LOG_LEVEL overrides the stderr_level parameter."""
    monkeypatch.setenv("QUARRY_LOG_LEVEL", "DEBUG")
    with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
        configure_logging(stderr_level="WARNING")
    config = mock_dc.call_args[0][0]
    assert config["handlers"]["stderr"]["level"] == "DEBUG"


def test_invalid_env_var_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid QUARRY_LOG_LEVEL is ignored; parameter value is used."""
    monkeypatch.setenv("QUARRY_LOG_LEVEL", "NONSENSE")
    with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
        configure_logging(stderr_level="WARNING")
    config = mock_dc.call_args[0][0]
    assert config["handlers"]["stderr"]["level"] == "WARNING"


def test_no_env_var_uses_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without QUARRY_LOG_LEVEL, the parameter controls stderr level."""
    monkeypatch.delenv("QUARRY_LOG_LEVEL", raising=False)
    with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
        configure_logging(stderr_level="INFO")
    config = mock_dc.call_args[0][0]
    assert config["handlers"]["stderr"]["level"] == "INFO"


def test_third_party_loggers_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Third-party loggers are pinned at WARNING to prevent DEBUG floods."""
    monkeypatch.delenv("QUARRY_LOG_LEVEL", raising=False)
    with patch("quarry.logging_config.logging.config.dictConfig") as mock_dc:
        configure_logging()
    config = mock_dc.call_args[0][0]
    for name in ("lancedb", "onnxruntime", "httpx"):
        assert config["loggers"][name]["level"] == "WARNING"
