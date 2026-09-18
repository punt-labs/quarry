"""Resolve the agent handle from the ethos repo pin at a given directory.

The pin names the identity a *session* runs as — the leader of the repo it is
opened in. Every producer that runs for the parent session (PreCompact,
SessionEnd, the memory doctor check) resolves through here; a producer that
runs for a subagent must not, because a subagent's working directory is the
repo and the pin would name the leader.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import final

import yaml

from quarry.ethos_tree import EthosTree

logger = logging.getLogger(__name__)


@final
class EthosConfig:
    """Ancestor-walking reader of the ethos repo pin's ``agent`` field."""

    __slots__ = ()

    @staticmethod
    def agent_handle_at(cwd: str) -> str:
        """Return the nearest ancestor pin's ``agent`` field, else ``""``.

        Empty string is the documented "no identity here" signal — callers use
        it as a gate, not as a value. Missing file, unparsable YAML, missing/
        blank/non-string ``agent``, and OS errors on read all funnel to ``""``.
        """
        for pin in EthosTree(cwd).pin_files():
            handle = EthosConfig._read_handle(pin)
            if handle is not None:
                return handle
        return ""

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
        agent = data.get("agent", "") if isinstance(data, dict) else ""
        return agent if isinstance(agent, str) else ""
