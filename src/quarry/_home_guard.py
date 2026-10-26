"""Stop, saying why, when the home directory cannot be resolved.

Several modules the CLI imports resolve the home directory when they are
imported, so on a host where it cannot be resolved (no ``HOME`` and no passwd
entry) importing the CLI fails before any command runs.  ``quarry doctor`` and
``quarry install`` are what a person runs to find out what is wrong, so they
must answer.  ``quarry.__main__`` imports this module FIRST: it answers for them,
as the failed, required check it is, and exits non-zero.  Because the check runs
at import, it covers the console script and ``python -m quarry`` alike.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import final

_NO_HOME = (
    "Home directory: quarry cannot determine the home directory "
    "(set HOME, or give this user a passwd entry)"
)


@final
class HomeGuard:
    """Check, once, that the home directory resolves."""

    __slots__ = ()

    @staticmethod
    def check() -> bool:
        """Return True when home resolves; otherwise report it and exit 1."""
        try:
            Path.home()
        except RuntimeError:
            diagnosing = any(arg in {"doctor", "install"} for arg in sys.argv[1:])
            if diagnosing:
                sys.stdout.write(f"  ✗ {_NO_HOME}\n")  # a failed, required check
            else:
                sys.stderr.write(f"quarry: {_NO_HOME}\n")
            sys.exit(1)
        return True


HOME_RESOLVED = HomeGuard.check()
