"""Connect Quarry process entry points to the shared durable diagnostic sink."""

from __future__ import annotations

import logging
import os
import sys
from typing import Self, final

from punt_observe.logging import LoggingSetupError, Role, configure_logging

from quarry.home_paths import HomeUnresolvedError, QuarryHome


@final
class LoggingConfig:
    """Select the process role and stderr threshold without owning any handlers."""

    __slots__ = ("_role", "_stderr_level")

    _role: Role
    _stderr_level: str
    _QUIET = (
        "mcp",
        "httpx",
        "httpcore",
        "urllib3",
        "asyncio",
        "lancedb",
        "onnxruntime",
    )

    def __new__(cls, role: Role, *, stderr_level: str = "WARNING") -> Self:
        """Select the process role and its default terminal threshold."""
        self = super().__new__(cls)
        self._role = role
        self._stderr_level = stderr_level
        return self

    @classmethod
    def daemon(cls) -> None:
        """Configure diagnostics for the daemon entry point."""
        cls(Role.DAEMON).configure()

    @classmethod
    def hook(cls) -> None:
        """Configure diagnostics before a hook dispatches its handler."""
        cls(Role.HOOK).configure()

    def configure(self) -> None:
        """Open the shared sink, or report why durable diagnostics are unavailable."""
        override = os.environ.get("QUARRY_LOG_LEVEL", "").upper()
        level = (
            override
            if override in logging.getLevelNamesMapping()
            else self._stderr_level
        )
        try:
            QuarryHome.home()
            configure_logging(
                "quarry",
                self._role,
                stderr_level=level,
                quiet=self._QUIET,
            )
        except HomeUnresolvedError:
            sys.stderr.write("quarry: no home directory; durable logs unavailable\n")
        except LoggingSetupError:
            sys.stderr.write(
                "quarry: cannot establish private diagnostics "
                "in ~/.punt-labs/quarry/logs\n"
            )
