"""The ``quarry`` entry points: run the command, or say why it cannot run.

The home directory is resolved when a path is wanted, never at import, so on a
host where it cannot be resolved (no ``HOME`` and no passwd entry) the command
that needs it fails with ``Path.home()``'s ``RuntimeError``.  Every entry point
(``quarry``, ``quarryd``, ``quarry-hook``, ``python -m quarry``) runs its command
through :meth:`CliEntry.run`, which turns that one error into one actionable line
on stderr and exit 1, never a traceback.  A hook in particular must never print
one.  ``quarry doctor`` and ``quarry install`` do not need this: they report the
same fault as a failed required check, "Home directory", and finish.
"""

from __future__ import annotations

import importlib
import sys
from typing import TYPE_CHECKING, final

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
    def is_home_failure(exc: BaseException) -> bool:
        """Return whether *exc* is ``Path.home()`` failing to find a home."""
        return isinstance(exc, RuntimeError) and "home directory" in str(exc).lower()

    @classmethod
    def run(cls, command: Callable[[], object]) -> None:
        """Run *command*; report an unresolvable home directory and exit 1.

        ``doctor`` and ``install`` get it as the failed, required check
        "Home directory" on stdout; every other command, the daemon and the hooks
        get one line on stderr.
        """
        try:
            command()
        except RuntimeError as exc:
            if not cls.is_home_failure(exc):
                raise
            if any(arg in {"doctor", "install"} for arg in sys.argv[1:]):
                sys.stdout.write(f"  ✗ Home directory: {_NO_HOME}\n")
            else:
                sys.stderr.write(f"quarry: {_NO_HOME}\n")
            sys.exit(1)

    @classmethod
    def quarry(cls) -> None:
        """Run the ``quarry`` CLI (the console script's target)."""
        cls.run(importlib.import_module("quarry.__main__").app)


main = CliEntry.quarry
