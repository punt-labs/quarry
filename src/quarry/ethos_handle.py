"""Resolve the agent handle from the ethos sidecar config at a given directory.

Sole caller today: the ``memory`` doctor check asks whether the current repo
has an ethos identity active. ``hooks.py`` still carries its own inline walker
that a follow-up unit migrates onto this helper; keeping the walker in one
place from the start prevents drift once the write path lands here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import final

import yaml

logger = logging.getLogger(__name__)

_ETHOS_CONFIG = Path(".punt-labs") / "ethos" / "config.yaml"


@final
class EthosConfig:
    """Ancestor-walking reader of the ``.punt-labs/ethos/config.yaml`` sidecar."""

    __slots__ = ()

    @staticmethod
    def agent_handle_at(cwd: str) -> str:
        """Return the nearest ancestor config's ``agent`` field, else ``""``.

        Empty string is the documented "no identity here" signal — callers use
        it as a gate, not as a value. Missing file, unparsable YAML, missing/
        blank/non-string ``agent``, and OS errors on read all funnel to ``""``.
        """
        for config_path in EthosConfig._walk_up(Path(cwd).resolve()):
            handle = EthosConfig._read_handle(config_path)
            if handle is not None:
                return handle
        return ""

    @staticmethod
    def _walk_up(start: Path) -> list[Path]:
        """Return every candidate config path from *start* to the FS root."""
        paths: list[Path] = []
        current = start
        while True:
            paths.append(current / _ETHOS_CONFIG)
            parent = current.parent
            if parent == current:
                return paths
            current = parent

    @staticmethod
    def _read_handle(config_path: Path) -> str | None:
        """Return the handle at *config_path*, or ``None`` when the file is absent.

        The absent-file signal is ``None`` (walk keeps going); any parse
        problem or missing/blank field short-circuits to ``""`` (walk stops).
        """
        if not config_path.is_file():
            return None
        try:
            data = yaml.safe_load(config_path.read_text())
        except (OSError, yaml.YAMLError):
            logger.warning(
                "ethos_handle: could not parse %s", config_path, exc_info=True
            )
            return ""
        if isinstance(data, dict):
            agent = data.get("agent", "")
            if isinstance(agent, str) and agent:
                return agent
        return ""
