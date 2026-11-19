"""Select the CLI-family role without reconfiguring a detached worker."""

from __future__ import annotations

from typing import ClassVar, final

from punt_observe.logging import Role

from quarry.logging_config import LoggingConfig


@final
class CliDiagnostics:
    """Own role selection for Typer and its detached sync caller."""

    _worker: ClassVar[bool] = False

    @classmethod
    def configure(cls, command: str | None, *, stderr_level: str) -> None:
        """Configure Typer once, preserving an enclosing detached worker sink."""
        if not cls._worker:
            role = Role.MCP if command == "mcp" else Role.CLI
            LoggingConfig(role, stderr_level=stderr_level).configure()

    @classmethod
    def worker(cls) -> None:
        """Configure the worker before entering the nested CLI dispatch."""
        cls._worker = True
        LoggingConfig(Role.WORKER).configure()
