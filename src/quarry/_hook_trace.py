"""Shared infrastructure for every ``quarry-hook`` handler.

:class:`HookTrace` emits one INFO breadcrumb per invocation so config-off and
unparseable-payload skips remain visible in ``quarry.log``.
:class:`HookPayload` parses untrusted payload fields defensively.
:class:`ReadAdmission` groups settings, resolver lookup, and client-side
secret pre-scanning for ``HookAgent.post_read``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, final

from quarry._hook_admission import (
    HookInput as HookInput,
    ReadAdmission as ReadAdmission,
)

logger = logging.getLogger("quarry.hooks")

Outcome = Literal["capture", "skip", "error"]


@final
@dataclass(slots=True)
class HookTrace:
    """Accumulate entry-time state and emit one INFO line on exit.

    ``config`` is ``None`` until the handler has checked its config gate
    (``on``/``off``), and ``payload_ok`` is ``None`` until the payload
    has been parsed and validated.  Emitting before either has been
    tagged still renders — the missing dimension shows as ``?``.
    """

    _name: str
    _config: bool | None = field(default=None)
    _payload_ok: bool | None = field(default=None)

    def mark_config(self, *, on: bool) -> None:
        """Record whether the config gate is enabled for this hook."""
        self._config = on

    def mark_payload(self, *, ok: bool) -> None:
        """Record whether the payload parsed to a usable value."""
        self._payload_ok = ok

    def skip(self, reason: str) -> None:
        """Emit the outcome line for a no-op skip (config, payload, filter, dedup)."""
        if reason in {"cwd", "payload", "no-url", "no-digest"}:
            self.reject()
            return
        self._emit("skip", reason)

    @staticmethod
    def reject() -> None:
        """Reject an unusable payload without recording untrusted fields."""
        logger.warning("malformed_payload")

    def capture(self, detail: str = "") -> None:
        """Emit the outcome line for a successful capture (or send)."""
        self._emit("capture", detail)

    def error(self, reason: str) -> None:
        """Emit the outcome line for an error path (daemon down, extractor fail)."""
        self._emit("error", reason)

    def _emit(self, outcome: Outcome, detail: str) -> None:
        """Write one INFO line summarizing this invocation."""
        cfg = self._render(flag=self._config, on_str="on", off_str="off")
        pay = self._render(flag=self._payload_ok, on_str="Y", off_str="N")
        tail = f" -> {outcome}"
        if detail:
            tail = f"{tail}:{detail}"
        logger.info(
            "quarry.hooks: %s: entered (config=%s, payload_ok=%s)%s",
            self._name,
            cfg,
            pay,
            tail,
        )

    @staticmethod
    def _render(*, flag: bool | None, on_str: str, off_str: str) -> str:
        """Render a tri-state boolean as ``on``/``off``/``?``."""
        if flag is None:
            return "?"
        return on_str if flag else off_str


@final
class HookPayload:
    """Untrusted-payload parsers shared by every hook handler.

    Grouped as a class rather than free functions so a handler that
    imports one utility imports the whole vocabulary at once, and so a
    caller adding a new parser has a single home for it — the PY-OO-7
    guard against a modules-with-helpers drift.

    Every method is read-only and stateless; the class is a namespace
    and the ``PLR6301`` false-positive for methods that never touch
    ``self`` does not apply because these are declared ``@staticmethod``
    up front.
    """

    __slots__ = ()

    @staticmethod
    def as_str(value: object) -> str:
        """Return *value* when it is a ``str``, else ``""`` (treated as absent).

        A non-string payload field (``None``, a number) is MISSING, not a
        value.  Coercing with ``str()`` would forge a truthy ``"None"``
        or ``"123"`` that slips past an emptiness guard — producing a
        bogus ``session-None`` capture or a resolved phantom transcript
        path — so hook input is read defensively.
        """
        return value if isinstance(value, str) else ""

    @classmethod
    def as_dir(cls, value: object) -> str:
        """Return *value* only when it names an ABSOLUTE path, else ``""``.

        A blank or RELATIVE ``cwd`` is "unregistered", not the hook
        process's own directory: both resolve against the hook's cwd, so
        a relative value would auto-register the wrong tree or write a
        capture into the wrong checkout.  ``cwd`` is untrusted; only an
        absolute path names a real client directory.
        """
        cwd = cls.as_str(value)
        return cwd if cwd and Path(cwd).is_absolute() else ""

    resolve_jsonl = HookInput.resolve_jsonl
