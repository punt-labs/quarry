"""Per-project shadow-repo configuration parsed from ``config.md`` frontmatter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Self

from quarry._frontmatter import Frontmatter

_CONFIG_FILENAME = ".punt-labs/quarry/config.md"
_SHADOW_SUFFIX = "-quarry"
_YAML_TRUE = frozenset({"true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class ShadowConfig:
    """The ``shadow:`` block of a project's ``.punt-labs/quarry/config.md``.

    ``remote`` may be empty, in which case the effective remote is derived from
    the public repo's ``origin`` (see :meth:`derive_remote`).  Pushing captures
    to a network remote is opt-in, so ``enabled`` defaults to ``False``.
    """

    enabled: bool
    remote: str
    acknowledge_unverified: bool

    @classmethod
    def from_project(cls, directory: Path) -> Self | None:
        """Return the project's shadow config, or None when the block is absent.

        None is the documented "not configured" contract: the config file is
        missing, unreadable, or has no ``shadow:`` block.  A present-but-empty
        block yields a config with the field defaults, not None.
        """
        path = directory / _CONFIG_FILENAME
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        block = Frontmatter(text).block("shadow")
        if block is None:
            return None
        return cls(
            enabled=cls._bool(block, "enabled", default=False),
            remote=block.get("remote", "").strip().strip("\"'"),
            acknowledge_unverified=cls._bool(
                block, "acknowledge_unverified", default=False
            ),
        )

    @staticmethod
    def derive_remote(origin_url: str) -> str:
        """Derive ``<origin>-quarry`` by inserting the suffix before ``.git``.

        ``git@github.com:org/repo.git`` -> ``git@github.com:org/repo-quarry.git``.
        Returns ``""`` when *origin_url* is empty (nothing to derive from).
        """
        url = origin_url.strip()
        if not url:
            return ""
        if url.endswith(".git"):
            return f"{url[: -len('.git')]}{_SHADOW_SUFFIX}.git"
        return f"{url}{_SHADOW_SUFFIX}"

    @staticmethod
    def _bool(block: dict[str, str], key: str, *, default: bool) -> bool:
        """Parse a YAML boolean alias; fail closed on an unrecognized value."""
        val = block.get(key)
        if val is None:
            return default
        # Anything not in the true-set (an explicit false alias or garbage)
        # fails closed to False.
        return val.lower() in _YAML_TRUE
