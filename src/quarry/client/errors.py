"""Typed client-side errors and the wire-status classifier.

Layer 2 (client transport) never imports presentation: these errors carry the
structured fields the command layer needs to render a message and pick an exit
code, but they never touch ``typer`` or ``rich``.  Three cohesive types cover
every failure the CLI dispatches on — a connection failure (autostart nudge), an
HTTP-status failure (the ``status`` selects the exit code; 409 is
"already in progress"), and the base error for a malformed/unparseable response.
The single classifier :meth:`QuarryError.from_response` turns a non-2xx status
into an :class:`HttpError`.

These are ``@dataclass(eq=False)`` rather than ``frozen``: an exception must let
the interpreter set ``__traceback__``/``__cause__`` as it propagates (a frozen
``__setattr__`` blocks that), so identity equality — not frozen value semantics —
is the right contract for an error.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import final

from quarry.net import LoopbackPolicy

# The 409 conflict carries a ``task_id`` the CLI polls; the command layer maps it
# to exit 0 ("already in progress").
CONFLICT_STATUS = 409


@dataclass(eq=False)
class QuarryError(Exception):
    """Base client failure — also the concrete error for a malformed response.

    Carries the human-readable ``message`` the command layer renders; the
    connection and HTTP subclasses add a ``target`` or wire ``status``.
    """

    _message: str

    @property
    def message(self) -> str:
        return self._message

    @property
    def status(self) -> int:
        # 0 = no HTTP status (a connection or protocol failure); :class:`HttpError`
        # overrides this with the real wire status.
        return 0

    def __str__(self) -> str:
        return self._message

    @classmethod
    def from_response(cls, status: int, body: object) -> HttpError:
        """Return the :class:`HttpError` for a non-2xx *status* and parsed *body*.

        ``body`` is a wire boundary — a decoded ``{"error": ...}`` mapping when
        the server emitted one, else any JSON value or ``None``.  The 409
        conflict's ``task_id`` is carried through for the CLI's poll hint.
        """
        return HttpError(cls._detail(status, body), status, cls._conflict_task_id(body))

    @staticmethod
    def _detail(status: int, body: object) -> str:
        """Return the server's error string, or a generic ``HTTP {status}``."""
        if isinstance(body, Mapping):
            error = body.get("error")
            if isinstance(error, str) and error:
                return error
        return f"HTTP {status}"

    @staticmethod
    def _conflict_task_id(body: object) -> str:
        """Return the ``task_id`` a 409 conflict carries, else an empty string."""
        if isinstance(body, Mapping):
            task_id = body.get("task_id")
            if isinstance(task_id, str):
                return task_id
        return ""


@final
@dataclass(eq=False)
class QuarryConnectionError(QuarryError):
    """The socket refused, DNS failed, or the TLS handshake could not complete.

    Raised before any HTTP status exists — including when a ``wss://`` target has
    no pinned CA — so a connection failure never propagates as a bare
    ``OSError``/``SystemExit``.  ``target`` names the address that could not be
    reached, for the CLI's autostart nudge.
    """

    _target: str

    @property
    def target(self) -> str:
        return self._target

    @property
    def is_loopback(self) -> bool:
        """Whether the unreachable target is a loopback address.

        ``target`` may be a URL (``http://127.0.0.1:8420``) or a bare host
        (``127.0.0.1``); the CLI shows the local autostart hint only when this is
        True — a remote failure must not suggest starting a local daemon.
        """
        host = urllib.parse.urlparse(self._target).hostname or self._target
        return LoopbackPolicy(host).is_loopback


@final
@dataclass(eq=False)
class HttpError(QuarryError):
    """A non-2xx HTTP response — ``status`` selects the CLI exit code.

    401/404/413/415/422/5xx all render ``Error: {message}`` and exit 1; a 409
    conflict exits 0 ("already in progress") and carries the running ``task_id``.
    The named-per-status distinction is intentionally not modeled as separate
    classes: the CLI dispatches on ``status``, and the daemon's own message
    already states the specific cause (e.g. "No registration found for …").
    """

    _status: int
    _task_id: str

    @property
    def status(self) -> int:
        return self._status

    @property
    def task_id(self) -> str:
        # Empty unless the server returned a 409 conflict naming a running task.
        return self._task_id
