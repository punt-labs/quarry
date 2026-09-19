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
    def subagent_handle_at(agent_type: str, cwd: str) -> str:
        """Return the identity a subagent's capture is attributed to, else ``""``.

        ``agent_type`` is the hook's statement of who ran and is used **iff**
        it names a registered identity (a ``<handle>.yaml`` in the vendored or
        global tree); a bare ``Agent()`` reviewer such as ``general-purpose``
        is filed unattributed rather than under a handle no ``find`` will ask
        for — or under the leader, whose memory it is not. An absent
        ``agent_type`` falls back to the repo pin, as every parent-session
        producer does.
        """
        if not agent_type:
            return EthosConfig.agent_handle_at(cwd)
        return agent_type if EthosTree(cwd).identity_exists(agent_type) else ""

    @staticmethod
    def _read_handle(config_path: Path) -> str | None:
        """Return the handle at *config_path*, or ``None`` when the file is absent.

        The pin is read through :meth:`EthosTree.read_sidecar`, so a symlinked
        pin (or ``.punt-labs``) is refused, never followed out of the checkout.
        The absent-file signal is ``None`` (walk keeps going); a refused
        symlink, any other read failure, a parse problem, or a missing/blank
        field short-circuits to ``""`` (walk stops, unattributed).
        """
        try:
            data = yaml.safe_load(EthosTree.read_sidecar(config_path))
        except FileNotFoundError:
            return None
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("ethos_handle: could not read %s: %s", config_path, exc)
            return ""
        agent = data.get("agent", "") if isinstance(data, dict) else ""
        return agent if isinstance(agent, str) else ""
