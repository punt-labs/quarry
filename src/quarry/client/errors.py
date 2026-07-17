"""Typed client-side errors and the wire-status classifier.

Layer 2 (client transport) never imports presentation: these errors carry the
structured fields the command layer needs to render a message and pick an exit
code, but they never touch ``typer`` or ``rich``.  A single classifier
(:meth:`QuarryError.from_response`) turns a non-2xx wire status into the right
leaf; a socket failure surfaces as :class:`QuarryConnectionError` before any
status exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import final

_AUTH_STATUS = 401
_NOT_FOUND_STATUS = 404
# The 409 sync-conflict carries a ``task_id`` the CLI polls; it is a
# BadRequestError the command layer maps to exit 0 ("already in progress").
CONFLICT_STATUS = 409
_CLIENT_ERROR_FLOOR = 400
_SERVER_ERROR_FLOOR = 500


@dataclass(frozen=True, slots=True)
class QuarryError(Exception):
    """Base for every client-side failure — never raised directly.

    Carries the human-readable ``message`` the command layer renders; leaves add
    a wire ``status`` or a connection ``target`` where relevant.
    """

    _message: str

    @property
    def message(self) -> str:
        return self._message

    def __str__(self) -> str:
        return self._message

    @classmethod
    def from_response(cls, status: int, body: object) -> QuarryError:
        """Return the error leaf for a non-2xx *status* and its parsed *body*.

        ``body`` is a wire boundary — a decoded ``{"error": ...}`` mapping when
        the server emitted one, else any JSON value or ``None``.  The 409
        conflict's ``task_id`` is carried through on :class:`BadRequestError`.
        """
        detail = cls._detail(status, body)
        if status == _AUTH_STATUS:
            return AuthError(detail)
        if status == _NOT_FOUND_STATUS:
            return NotFoundError(detail, status)
        if _CLIENT_ERROR_FLOOR <= status < _SERVER_ERROR_FLOOR:
            return BadRequestError(detail, status, cls._conflict_task_id(body))
        return ServerError(detail, status)

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
@dataclass(frozen=True, slots=True)
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


@final
@dataclass(frozen=True, slots=True)
class AuthError(QuarryError):
    """HTTP 401 — the presented bearer token was missing, stale, or rejected."""


@final
@dataclass(frozen=True, slots=True)
class NotFoundError(QuarryError):
    """HTTP 404 — the requested document, collection, or registration is absent."""

    _status: int

    @property
    def status(self) -> int:
        return self._status


@final
@dataclass(frozen=True, slots=True)
class BadRequestError(QuarryError):
    """HTTP 400/409/413/415/422 — the request was rejected as malformed or in conflict.

    A 409 sync/optimize/backfill conflict carries the running task's ``task_id``
    so the command layer can tell the user which task to poll.
    """

    _status: int
    _task_id: str = ""

    @property
    def status(self) -> int:
        return self._status

    @property
    def task_id(self) -> str:
        # Empty unless the server returned a 409 conflict naming a running task.
        return self._task_id


@final
@dataclass(frozen=True, slots=True)
class ServerError(QuarryError):
    """HTTP 5xx (or any other unexpected status) — the daemon failed internally."""

    _status: int

    @property
    def status(self) -> int:
        return self._status


@final
@dataclass(frozen=True, slots=True)
class ProtocolError(QuarryError):
    """The response body was not JSON, not an object, or failed model validation."""
