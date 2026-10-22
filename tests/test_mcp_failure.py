"""Tests for quarry.mcp_failure: one failure, logged and rendered alike."""

from __future__ import annotations

import logging

import pytest

from quarry.client import HttpError, QuarryConnectionError
from quarry.mcp_failure import ToolFailure


class TestRender:
    def test_a_daemon_refusal_renders_its_message_alone(self) -> None:
        exc = HttpError("binary content rejected: pdf header", 400, "")
        assert ToolFailure(exc).render() == "Error: binary content rejected: pdf header"

    def test_a_down_daemon_renders_the_recovery_hint(self) -> None:
        exc = QuarryConnectionError("refused", "http://127.0.0.1:8420")
        assert ToolFailure(exc).render() == (
            "Error: refused — Restart it: 'systemctl --user restart quarry' (Linux) "
            "or 'launchctl kickstart -k gui/$(id -u)/com.punt-labs.quarry' (macOS), "
            "then retry. No results were returned for this call."
        )

    def test_a_remote_daemon_renders_the_remote_hint(self) -> None:
        exc = QuarryConnectionError("refused", "https://quarry.example.com")
        assert ToolFailure(exc).render() == (
            "Error: refused — This is a remote target — restarting a local daemon "
            "will not help. Check QUARRY_URL / 'quarry login', then retry."
        )

    def test_any_other_exception_names_its_class(self) -> None:
        assert ToolFailure(RuntimeError("boom")).render() == "Error: RuntimeError: boom"


class TestLogging:
    @pytest.mark.parametrize("status", [400, 404, 409, 499])
    def test_a_4xx_is_a_quiet_warning(self, status: int) -> None:
        failure = ToolFailure(HttpError("no", status, ""))
        assert failure.level == logging.WARNING
        assert failure.exc_info is None

    @pytest.mark.parametrize("status", [500, 503])
    def test_a_5xx_is_an_error_with_its_traceback(self, status: int) -> None:
        exc = HttpError("down", status, "")
        failure = ToolFailure(exc)
        assert failure.level == logging.ERROR
        assert failure.exc_info is exc

    def test_any_other_exception_is_an_error_with_its_traceback(self) -> None:
        exc = ValueError("x")
        assert ToolFailure(exc).exc_info is exc
