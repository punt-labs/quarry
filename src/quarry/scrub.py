"""Scrub secrets, PII, and profanity from text before writing to disk.

Regex-driven redaction replaces matches with labeled markers like
``[REDACTED:gh-pat]`` so a downstream auditor can tell *what* was
removed without seeing the value.  Markers are themselves never matched,
so the scrubber is idempotent (running twice yields the same output).

Categories:

| Category          | What it catches                                                |
|-------------------|----------------------------------------------------------------|
| gh-pat            | GitHub PATs: ghp_, ghs_, ghu_, gho_, ghr_                     |
| aws-access-key    | AKIA + 16 alnum                                                |
| aws-secret-key    | 40-char base64 only on a line mentioning aws_secret_access_key |
| anthropic-key     | sk-ant-...                                                     |
| openai-key        | sk-... (excluding sk-ant-...)                                  |
| bearer            | ``Authorization: Bearer ...`` headers                          |
| jwt               | three-segment ``eyJ...`` tokens                                |
| pem-private-key   | multi-line ``-----BEGIN ... PRIVATE KEY-----`` blocks          |
| gpg-private-key   | multi-line PGP private key blocks                              |
| env-secret        | KEY=value where KEY contains TOKEN/SECRET/PASSWORD/etc.        |
| slack-token       | xox[baprs]-... tokens                                          |
| path              | home directories: /Users/<user>/ and /home/<user>/ -> ~/       |
| email             | any RFC-shaped address -> [REDACTED:email]                     |
| hostname          | the local machine hostname (socket.gethostname())             |
| profanity         | words from a configurable word list                            |

The PII passes (path, email, hostname) run write-time so filesystem
paths, email addresses, and the operator's machine name never reach a
git-committed capture file.  Ordering is load-bearing: email precedes
hostname so a hostname inside an email domain is subsumed by the whole
-email redaction rather than half-redacted, which would leak the local
part.
"""

from __future__ import annotations

import logging
import re
import socket
from collections import Counter
from dataclasses import dataclass
from re import Pattern
from typing import Self

from quarry.scrub_rules import (
    AWS_SECRET_LINE_HINT,
    BLOCK_RULES,
    LINE_RULES,
    SecretRule,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default profanity list
# ---------------------------------------------------------------------------

DEFAULT_PROFANITY: tuple[str, ...] = (
    "fuck",
    "shit",
    "asshole",
    "damn",
    "dick",
    "jerk",
    "ass",
    "moron",
    "idiot",
    "stupid",
    "dumb",
    "imbecile",
    "cretin",
    "bastard",
    "bitch",
    "crap",
    "piss",
    "hell",
    "douche",
)


# ---------------------------------------------------------------------------
# PII patterns
# ---------------------------------------------------------------------------

# Home directories for any user. Case-sensitive (macOS /Users, Linux /home);
# no re.IGNORECASE so /users/ inside a URL is not over-matched. Only the
# username segment (``[^/\s]+``) is consumed, so deeper path structure — the
# useful part of a capture — is retained. ``/root`` is intentionally excluded:
# no username to generalize, and it is not the PII class this targets.
_PATH_RE = re.compile(r"(?:/Users|/home)/[^/\s]+")

# RFC-shaped email. The lookbehind keeps the match from starting inside a longer
# token; the trailing ``(?!\w)`` only rejects a match that would continue into
# another word character, so a sentence-final ``jmf@pobox.com.`` still redacts
# (the ``.`` is not ``\w``) — excluding ``.``/``-`` from the trailing set here
# was a leak, since a period follows an address in the most common prose context.
# Multi-label TLDs (``jmf@pobox.co.uk``) still match: the engine backtracks the
# greedy domain so ``\.[A-Za-z]{2,}`` lands on the final label. The
# ``[REDACTED:email]`` marker has no ``@``, so a scrubbed address cannot re-match.
# Accepted limit: the ASCII character classes do not match unicode/IDN addresses
# (e.g. ``用户@例え.jp``); over-matching ``git@github.com:org/repo.git`` SSH remotes
# is over-redaction, not a leak.
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?!\w)"
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScrubConfig:
    """Scrubber configuration.  Frozen for safe sharing across calls."""

    scrub_secrets: bool = True
    scrub_profanity: bool = True
    scrub_pii: bool = True
    profanity_words: tuple[str, ...] = DEFAULT_PROFANITY
    email_placeholder: str = "[REDACTED:email]"
    hostname_placeholder: str = "[REDACTED:hostname]"
    # None -> resolve the local hostname via socket.gethostname() at build time.
    # Tests inject an explicit value so results do not depend on the CI machine.
    local_hostname: str | None = None


# ---------------------------------------------------------------------------
# Scrubber
# ---------------------------------------------------------------------------


class Scrubber:
    """Owns the compiled rules and applies every redaction pass in order.

    A single instance holds the secret rules, the profanity regex, the PII
    patterns, and the resolved local-hostname regex, so a call is just
    ``Scrubber(config).scrub(text)``.  The module-level ``scrub`` and
    ``scrub_and_log`` functions are thin shims over a shared default
    instance — callers keep their existing signatures.
    """

    __slots__ = (
        "_block_rules",
        "_config",
        "_email_re",
        "_host_re",
        "_line_rules",
        "_path_re",
        "_profanity_re",
    )

    _config: ScrubConfig
    _block_rules: tuple[SecretRule, ...]
    _line_rules: tuple[SecretRule, ...]
    _path_re: Pattern[str]
    _email_re: Pattern[str]
    _profanity_re: Pattern[str] | None
    _host_re: Pattern[str] | None

    def __new__(cls, config: ScrubConfig | None = None) -> Self:
        self = super().__new__(cls)
        self._config = config if config is not None else ScrubConfig()
        self._block_rules = BLOCK_RULES
        self._line_rules = LINE_RULES
        self._path_re = _PATH_RE
        self._email_re = _EMAIL_RE
        self._profanity_re = self._build_profanity_re()
        self._host_re = self._build_host_re()
        return self

    @property
    def config(self) -> ScrubConfig:
        return self._config

    def scrub(self, text: str) -> tuple[str, dict[str, int]]:
        """Scrub *text*.  Return ``(scrubbed_text, redaction_counts)``.

        Counts only include categories that fired at least once.  The pass
        order is secrets -> paths -> emails -> hostname -> profanity; email
        must precede hostname (see the module docstring).
        """
        counts: Counter[str] = Counter()
        if not text:
            return text, dict(counts)

        cfg = self._config
        if cfg.scrub_secrets:
            text = self._scrub_block_secrets(text, counts)
            text = "".join(
                self._scrub_line_secrets(line, counts)
                for line in text.splitlines(keepends=True)
            )

        if cfg.scrub_pii:
            text = self._scrub_paths(text, counts)
            text = self._scrub_emails(text, counts)
            text = self._scrub_hostname(text, counts)

        if cfg.scrub_profanity:
            text = self._scrub_profanity(text, counts)

        return text, dict(counts)

    def _scrub_block_secrets(self, text: str, counts: Counter[str]) -> str:
        """Apply whole-document redactions (PEM/GPG/env-secret)."""
        for rule in self._block_rules:
            new_text, n = rule.pattern.subn(rule.replacement(), text)
            if n:
                counts[rule.category] += n
                text = new_text
        return text

    def _scrub_line_secrets(self, line: str, counts: Counter[str]) -> str:
        """Apply per-line redactions, honoring rule order and context."""
        for rule in self._line_rules:
            if rule.category == "aws-secret-key" and not AWS_SECRET_LINE_HINT.search(
                line
            ):
                continue
            new_line, n = rule.pattern.subn(rule.replacement(), line)
            if n:
                counts[rule.category] += n
                line = new_line
        return line

    def _scrub_paths(self, text: str, counts: Counter[str]) -> str:
        """Replace home directories with ``~`` (the trailing slash is kept)."""
        new_text, n = self._path_re.subn("~", text)
        if n:
            counts["path"] += n
        return new_text

    def _scrub_emails(self, text: str, counts: Counter[str]) -> str:
        """Replace RFC-shaped addresses with the email placeholder."""
        new_text, n = self._email_re.subn(self._config.email_placeholder, text)
        if n:
            counts["email"] += n
        return new_text

    def _scrub_hostname(self, text: str, counts: Counter[str]) -> str:
        """Redact the local machine hostname, if one was resolved."""
        if self._host_re is None:
            return text
        new_text, n = self._host_re.subn(self._config.hostname_placeholder, text)
        if n:
            counts["hostname"] += n
        return new_text

    def _scrub_profanity(self, text: str, counts: Counter[str]) -> str:
        """Replace whole-word profanity matches with the marker."""
        if self._profanity_re is None:
            return text
        new_text, n = self._profanity_re.subn("[REDACTED:profanity]", text)
        if n:
            counts["profanity"] += n
        return new_text

    def _build_profanity_re(self) -> Pattern[str] | None:
        """Compile a whole-word regex covering the config's profanity words.

        Returns ``None`` if the list is empty.
        """
        words = self._config.profanity_words
        cleaned = sorted({w.strip().lower() for w in words if w.strip()})
        if not cleaned:
            return None
        body = "|".join(re.escape(w) for w in cleaned)
        return re.compile(rf"\b(?:{body})\b", re.IGNORECASE)

    def _build_host_re(self) -> Pattern[str] | None:
        """Compile a whole-word regex for the local hostname and its forms.

        A ``None`` ``local_hostname`` resolves the live hostname via
        ``socket.gethostname()``.  The forms are the full hostname, the name
        without a trailing ``.local`` (mDNS), and the short leaf when it is at
        least four characters.  Accepted limit: the length guard prevents
        redacting a 2-3 char leaf that collides with a common word, but a leaf
        ≥4 chars that happens to be an English word can still over-redact — an
        accepted tradeoff (closing the leak direction wins for a security
        property).  Matching is case-insensitive because DNS/mDNS names are
        (``Jims-MacBook-Pro`` and ``jims-macbook-pro`` name the same host).
        Returns ``None`` when no usable form exists.
        """
        hostname = self._config.local_hostname
        host = hostname if hostname is not None else socket.gethostname()
        forms = {host}
        if host.endswith(".local"):
            forms.add(host[: -len(".local")])
        leaf = host.split(".", 1)[0]
        if len(leaf) >= 4:
            forms.add(leaf)
        usable = sorted((f for f in forms if f), key=len, reverse=True)
        if not usable:
            return None
        body = "|".join(re.escape(f) for f in usable)
        return re.compile(rf"\b(?:{body})\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Shared default scrubber and public shims
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = ScrubConfig()
_DEFAULT_SCRUBBER = Scrubber(_DEFAULT_CONFIG)


def scrub(text: str, config: ScrubConfig | None = None) -> tuple[str, dict[str, int]]:
    """Scrub *text* per *config*.  Return ``(scrubbed_text, redaction_counts)``.

    A ``None`` config uses the shared default scrubber (secrets, PII, and
    profanity all on, live local hostname resolved).
    """
    if config is None:
        return _DEFAULT_SCRUBBER.scrub(text)
    return Scrubber(config).scrub(text)


def scrub_and_log(text: str, label: str) -> str:
    """Scrub *text* and log redaction counts at INFO level.

    *label* identifies the call site in the log message (e.g.
    ``"pre-compact"`` or ``"backfill"``).  Returns the scrubbed text.
    """
    scrubbed, counts = _DEFAULT_SCRUBBER.scrub(text)
    if counts:
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        logger.info("%s: scrubbed capture file (%s)", label, summary)
    return scrubbed
