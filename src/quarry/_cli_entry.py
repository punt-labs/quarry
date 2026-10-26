"""The ``quarry`` entry points: run the command, or say why it cannot run.

The home directory is resolved when a path is wanted, never at import, so on a
host where it cannot be resolved (no ``HOME`` and no passwd entry) the command
that needs it fails with :class:`~quarry.home_paths.HomeUnresolvedError`.  Every
entry point (``quarry``, ``quarryd``, ``quarry-hook``, ``python -m quarry``) runs
its command through :meth:`CliEntry.run`, which turns that one error, matched by
type, into one actionable line on stderr and exit 1, never a traceback.  A hook
in particular must never print one.  ``quarry doctor`` and ``quarry install``
run their real checks and steps regardless, and report the same fault as a
failed required check, "Home directory"; they exit 1 through their own summary.
"""

from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING, final

from quarry.home_paths import HomeUnresolvedError

if TYPE_CHECKING:
    from collections.abc import Callable

_NO_HOME = (
    "cannot determine the home directory (set HOME, or give this user a passwd entry)"
)


@final
class CliEntry:
    """Run an entry point's command with the home-directory failure made clean."""

    __slots__ = ()

    @staticmethod
    def run(command: Callable[[], object]) -> None:
        """Run *command*; report an unresolvable home directory and exit 1."""
        try:
            command()
        except HomeUnresolvedError:
            sys.stderr.write(f"quarry: {_NO_HOME}\n")
            sys.exit(1)

    @classmethod
    def quarry(cls) -> None:
        """Run the ``quarry`` CLI (the console script's target)."""
        cls.run(importlib.import_module("quarry.__main__").app)


main = CliEntry.quarry
