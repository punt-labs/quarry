"""Parse a PostToolUse Read payload and admit-filter it against the capture rules.

``Read`` fires far more often than any other hook this repo captures and has
the highest secret-leak surface — an out-of-tree ``.env``, an SSH private
key, an ``.aws/credentials`` file — so admission is fail-closed on four
independent checks:

1. **In-tree exclusion**: paths under a registered collection are already
   indexed by the session-start sync; a second capture is pure duplication.
2. **Secret-path denylist**: a fixed set of filename fragments (``.env``,
   ``id_rsa``, ``*.pem``, ``.ssh/*``, etc.) skip regardless of location.
3. **Extension allowlist**: only formats quarry's loaders recognise as prose
   (``.md``, ``.txt``, ``.rst``, ``.pdf``, ``.docx``) — an out-of-tree
   ``.py``/``.json``/``.log`` is not durable knowledge by default.
4. **Size cap**: reject content over a fixed byte cap so one large PDF read
   can't dominate the capture queue.

Any check failing → do not capture, no daemon call, no INFO logging (this
fires too often for that).  The ``ReadPayload`` parser is defensive in the
same way :class:`WebFetchPayload` and :class:`WebSearchPayload` are:
malformed input yields ``None`` per property, and the handler skips.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from quarry.collection_resolver import CollectionResolver

_MAX_BYTES = 200 * 1024

# Case-insensitive substring fragments: a basename containing ANY of these is
# secret regardless of stem or wrapping suffix, so "credentials.md" and
# "id_rsa.txt" are denied exactly like the bare files they're copies of.
_SECRET_BASENAME_FRAGMENTS = frozenset(
    {
        ".env",
        "credentials",
        "secrets",
        "passwords",
        "api-keys",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        ".pem",
        ".key",
        ".pfx",
        ".p12",
        "known_hosts",
        ".netrc",
    }
)

# Directory fragments: any path passing through one of these trees is secret
# regardless of the leaf filename.
_SECRET_DIR_FRAGMENTS = frozenset({".ssh/", ".aws/", ".gnupg/", ".kube/", ".docker/"})

# Prose/document extensions the loaders already know how to extract text
# from.  Lowered for case-insensitive matching.  Kept intentionally narrow
# in v1: log/source/config files are excluded until the operator opts in.
_ALLOWED_SUFFIXES = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".rst",
        ".pdf",
        ".docx",
        ".doc",
        ".pptx",
        ".html",
        ".htm",
    }
)


@dataclass(frozen=True, slots=True)
class ReadPayload:
    """A PostToolUse Read payload, parsed into what a capture needs.

    ``file_path`` is the read target; ``content`` is the file's text as it
    arrived in ``tool_response``.  Both return ``None`` on any absent or
    malformed field — absence is the documented contract, matching
    :class:`WebFetchPayload` and :class:`WebSearchPayload`.
    """

    _raw: dict[str, object]

    @property
    def file_path(self) -> str | None:
        """Return the ``file_path`` from ``tool_input``, or ``None``."""
        tool_input = self._raw.get("tool_input")
        if isinstance(tool_input, dict):
            path = tool_input.get("file_path")
            if isinstance(path, str) and path.strip():
                return path.strip()
        return None

    @property
    def content(self) -> str | None:
        """Return the file's text from ``tool_response``, or ``None``.

        ``tool_response`` may be a JSON-encoded string (Claude Code's usual
        wire form) or a dict already parsed by an upstream harness.
        """
        raw = self._raw.get("tool_response")
        parsed = self._as_parsed(raw)
        if isinstance(parsed, str) and parsed:
            return parsed
        if isinstance(parsed, dict):
            for key in ("content", "text", "result"):
                value = parsed.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    @staticmethod
    def _as_parsed(raw: object) -> object:
        """Return *raw* decoded from JSON if it's a string, else *raw* itself."""
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return raw  # a bare string body — return as-is
        return raw


@final
class ReadCaptureFilter:
    """Decide whether a Read payload should be captured.

    Four independent checks in order, fail-closed on any failure.  Held as
    one class rather than four free functions per PY-OO-7: each check
    operates on the same ``(file_path, cwd, content)`` vocabulary and
    together they name a single admission policy.
    """

    __slots__ = ("_resolver",)

    _resolver: CollectionResolver | None

    def __new__(cls, resolver: CollectionResolver | None = None) -> Self:
        self = super().__new__(cls)
        self._resolver = resolver
        return self

    def should_capture(
        self, file_path: str, cwd: str, content_bytes: int | None = None
    ) -> bool:
        """Return whether the four admission checks all pass.

        ``content_bytes`` is optional so the filter can be applied before the
        content is decoded; callers pass it once known and the size check
        runs against it.
        """
        if not file_path:
            return False
        path = Path(file_path)
        try:
            resolved = path.resolve()
        except (OSError, ValueError):
            return False
        if self._is_in_tree(resolved, cwd):
            return False
        if self._is_secret_path(resolved):
            return False
        if not self._is_allowed_suffix(path):
            return False
        return not (content_bytes is not None and content_bytes > _MAX_BYTES)

    def _is_in_tree(self, resolved_path: Path, cwd: str) -> bool:
        """Return whether *resolved_path* is under a registered collection for *cwd*."""
        if self._resolver is None or not cwd:
            return False
        try:
            registration = self._resolver.covering_registration(cwd)
        except OSError:
            return False
        if registration is None:
            return False
        try:
            resolved_root = Path(registration.directory).resolve()
        except (OSError, ValueError):
            return False
        return resolved_path.is_relative_to(resolved_root)

    @classmethod
    def _is_secret_path(cls, resolved_path: Path) -> bool:
        """Return whether *resolved_path*'s basename or ancestry is a secret.

        Runs against the RESOLVED path, not the caller's raw one — a
        ``notes.md`` symlink pointing at ``.env`` must not evade the
        denylist by hiding behind an innocuous name.
        """
        posix_lower = resolved_path.as_posix().lower() + "/"
        if any(frag in posix_lower for frag in _SECRET_DIR_FRAGMENTS):
            return True
        name_lower = resolved_path.name.lower()
        return any(frag in name_lower for frag in _SECRET_BASENAME_FRAGMENTS)

    @classmethod
    def _is_allowed_suffix(cls, path: Path) -> bool:
        """Return whether *path*'s extension is in the prose allowlist."""
        return path.suffix.lower() in _ALLOWED_SUFFIXES
