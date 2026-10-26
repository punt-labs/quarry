"""Ensure opencv-python-headless is the cv2 provider in a Python environment."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from typing import Self, final

from quarry.doctor_failure import CheckFailure

__all__ = ["HeadlessOpenCv"]

_PACKAGE = "opencv-python-headless"


@final
class HeadlessOpenCv:
    """Force the headless OpenCV wheel to own ``cv2`` in a Python environment.

    rapidocr hard-requires the desktop ``opencv-python``; both wheels ship the
    same ``cv2`` package, so a plain ``pip install`` lets the desktop build
    shadow the headless one and fail to load on a screenless box. Reinstalling
    the headless wheel makes it the last writer of ``cv2/`` so it wins — the
    package-side equivalent of the installer's resolver override, run once by
    ``quarry install`` so every install path gets working headless OCR.
    """

    __slots__ = ("_python",)

    _python: str

    def __new__(cls, python: str) -> Self:
        self = super().__new__(cls)
        self._python = python
        return self

    def enforce(self) -> str:
        """Reinstall the headless wheel so it provides ``cv2``; return a status line.

        Raises ``RuntimeError`` (with pip's last line) if the reinstall fails; the
        caller (``quarry install``) treats that as a best-effort skip, and the
        runtime OCR guard still degrades cleanly.
        """
        done = subprocess.run(  # noqa: S603  # fixed argv, no shell, no user input
            self._reinstall_command(),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if done.returncode != 0:
            why = CheckFailure.because(done.stderr)
            msg = f"the reinstall exited {done.returncode}{why}"
            raise RuntimeError(msg)
        return f"{_PACKAGE} is the cv2 provider"

    def remediation(self) -> str:
        """Return the command :meth:`enforce` runs as a copy-pastable shell string.

        ``shlex.join`` quotes each argv element, so an interpreter path containing
        spaces yields a command that pastes and runs correctly.
        """
        return shlex.join(self._reinstall_command())

    def _reinstall_command(self) -> list[str]:
        """Return the force-reinstall argv, preferring uv (tool venvs lack pip).

        ``--no-deps`` keeps the reinstall to the one wheel; ``--force-reinstall``
        rewrites the shared ``cv2/`` files so the headless build wins even when
        it is already the (shadowed) satisfied requirement.
        """
        args = ["--force-reinstall", "--no-deps", _PACKAGE]
        uv = shutil.which("uv")
        if uv is not None:
            return [uv, "pip", "install", "--python", self._python, *args]
        return [self._python, "-m", "pip", "install", *args]
