"""Shared infrastructure for every ``quarry-hook`` handler.

Three responsibilities, all about making the handlers thin:

* :class:`HookTrace` emits one INFO breadcrumb per invocation so a
  silent-skip is visible in ``quarry.log`` — without it, a config-off or
  unparseable-payload skip returns ``{}`` and leaves no trace, and
  diagnosis becomes an inference chain (B-hooks integration report,
  gap G6).
* :class:`HookPayload` parses untrusted payload fields defensively —
  extracted from hooks_agent so the handler class stops accreting static
  helpers that don't share vocabulary with its own methods (PY-OO-7).
* :class:`ReadAdmission` groups the Read-specific admission pieces
  (settings + resolver lookup + client-side secret pre-scan) that
  ``HookAgent.post_read`` used to carry as static helpers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, final

from quarry.scrub import Scrubber, ScrubConfig

if TYPE_CHECKING:
    from quarry.collection_resolver import CollectionResolver
    from quarry.config import Settings

logger = logging.getLogger("quarry.hooks")

# Secrets-only scrubber, built once: a client-side defense-in-depth check on
# Read content ahead of the daemon's own scrub, relevant when QUARRY_URL
# points at a remote daemon.  PII/profanity passes are off — those aren't
# reasons to withhold a capture, only genuine secrets are.
_SECRET_SCRUBBER = Scrubber(ScrubConfig(scrub_pii=False, scrub_profanity=False))


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
        self._emit("skip", reason)

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

    @staticmethod
    def resolve_jsonl(path_str: str, *, label: str) -> Path | None:
        """Resolve *path_str* to a ``.jsonl`` transcript, else ``None``.

        Skip conditions match the no-op contract: an OS-invalid path or
        a non-``.jsonl`` suffix returns ``None``, never crashes the
        hook.
        """
        try:
            resolved = Path(path_str).resolve()
        except (OSError, ValueError):
            logger.warning("%s: unresolvable transcript_path", label, exc_info=True)
            return None
        if resolved.suffix != ".jsonl":
            logger.warning("%s: unexpected suffix %s", label, resolved.suffix)
            return None
        return resolved


@final
class ReadAdmission:
    """Read-hook admission helpers grouped by shared vocabulary.

    Three concerns the post-Read handler used to hold as static methods
    on ``HookAgent`` — settings load, resolver build, and client-side
    secret pre-scan — collect here so the handler class stays focused
    on orchestration only (PY-OO-7).
    """

    __slots__ = ()

    @staticmethod
    def content_has_secret(content: str) -> bool:
        """Return whether *content* matches a secret pattern (PEM/env/PAT/etc).

        Defense-in-depth ahead of the daemon's own scrub — catches an
        obvious secret before it leaves the machine, which matters when
        ``QUARRY_URL`` points at a remote daemon.
        """
        _, redactions = _SECRET_SCRUBBER.scrub(content)
        return bool(redactions)

    @classmethod
    def collection_resolver_for(cls, cwd: str) -> CollectionResolver | None:
        """Open a :class:`CollectionResolver`, or ``None`` on failure.

        Read fires often; a settings-load failure must never crash the
        handler.  ``None`` is the "no in-tree exclusion possible"
        contract that :class:`ReadCaptureFilter` treats as "resolver
        unavailable" — the first admission check skips, the remaining
        three still run.
        """
        del cwd  # resolver is settings-scoped; cwd flows to should_capture
        try:
            settings = cls._resolve_settings()
        except (OSError, ValueError):
            return None
        try:
            from quarry.collection_resolver import CollectionResolver  # noqa: PLC0415
            from quarry.sync_registry import SyncRegistry  # noqa: PLC0415

            conn = SyncRegistry(settings.registry_path)
        except (OSError, ValueError):
            return None
        return CollectionResolver(conn)

    @staticmethod
    def _resolve_settings() -> Settings:
        """Load settings resolved for the default database."""
        from quarry.config import Settings  # noqa: PLC0415

        return Settings.load().resolve_db_paths(None)
