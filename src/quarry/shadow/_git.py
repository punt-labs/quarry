"""A fail-open ``git``/``gh`` subprocess runner scoped to one working directory.

``GitRunner`` centralizes the shadow subsystem's subprocess policy: every call
runs in a fixed ``cwd``, captures output, never raises, and logs a non-zero
exit's ``stderr`` at debug level so a fail-open problem stays diagnosable.  It
holds no git domain knowledge — ``ShadowRepo`` composes it with the shadow
semantics (allowlist, refusal gates, visibility).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Self, final

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30


@final
class GitRunner:
    """Run a ``git``/``gh`` subprocess in one working directory, never raising."""

    __slots__ = ("_cwd",)

    _cwd: Path

    def __new__(cls, cwd: Path) -> Self:
        self = super().__new__(cls)
        self._cwd = cwd
        return self

    def run(self, argv: list[str]) -> tuple[int, str]:
        """Return ``(returncode, stripped_stdout)``; ``(1, "")`` on any failure.

        A non-zero exit logs the command's ``stderr`` at debug level so a
        fail-open git/gh problem stays diagnosable instead of vanishing.
        """
        try:
            proc = subprocess.run(  # noqa: S603
                argv,
                cwd=self._cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=_GIT_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("shadow: %s failed to launch: %s", argv[0], exc)
            return 1, ""
        if proc.returncode != 0:
            logger.debug(
                "shadow: %s exited %d: %s",
                argv[0],
                proc.returncode,
                proc.stderr.strip(),
            )
        return proc.returncode, proc.stdout.strip()

    def ok(self, argv: list[str]) -> bool:
        """Return whether *argv* exited zero."""
        return self.run(argv)[0] == 0
