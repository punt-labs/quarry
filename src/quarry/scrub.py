"""Scrub secrets and profanity from text before writing to disk.

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
| profanity         | words from a configurable word list                            |
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from re import Pattern

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
# Secret rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SecretRule:
    """A single regex-based secret detector.

    Whether a rule runs whole-document or per-line is determined by
    which tuple it lives in — see ``_build_secret_rules``.
    """

    category: str
    pattern: Pattern[str]
    replace: str | None = None


def _build_secret_rules() -> tuple[tuple[_SecretRule, ...], tuple[_SecretRule, ...]]:
    """Return ``(block_rules, line_rules)``.

    Block rules run once over the whole document (multi-line patterns).
    Line rules run per-line so context-sensitive detectors can inspect
    their own line.
    """
    block_rules = (
        # PGP before PEM — PGP is technically PEM-shaped.
        _SecretRule(
            category="gpg-private-key",
            pattern=re.compile(
                r"-----BEGIN PGP PRIVATE KEY BLOCK-----"
                r".*?"
                r"-----END PGP PRIVATE KEY BLOCK-----",
                re.DOTALL,
            ),
        ),
        _SecretRule(
            category="pem-private-key",
            pattern=re.compile(
                r"-----BEGIN (?:[A-Z][A-Z ]*)?PRIVATE KEY-----"
                r".*?"
                r"-----END (?:[A-Z][A-Z ]*)?PRIVATE KEY-----",
                re.DOTALL,
            ),
        ),
        # env-secret: KEY=value where KEY contains a secret-bearing suffix.
        # Every whitespace class is ``[ \t]`` — never ``\s`` — to prevent
        # cross-line matching in MULTILINE mode.
        _SecretRule(
            category="env-secret",
            pattern=re.compile(
                r"""(?xm)
                (?<![A-Za-z0-9_])
                ((?:export[ \t]+)?)
                (\w*(?:TOKEN|SECRET|PASSWORD|PASSPHRASE|API_KEY|ACCESS_KEY|PRIVATE_KEY|AUTH_KEY)\w*)
                ([ \t]*=[ \t]*)
                (?!\[REDACTED:)
                ([^\r\n]+?)
                [ \t]*
                (?=\r?$)
                """,
            ),
            replace=r"\1\2\3[REDACTED:env-secret]",
        ),
    )

    line_rules = (
        # aws-secret-key: 40-char base64 on a line mentioning the key name.
        _SecretRule(
            category="aws-secret-key",
            pattern=re.compile(
                r"(?<![A-Za-z0-9])(?!\[REDACTED)([A-Za-z0-9/+=]{40})(?![A-Za-z0-9])",
            ),
        ),
        _SecretRule(
            category="gh-pat",
            pattern=re.compile(r"\b(?:ghp|ghs|ghu|gho|ghr)_[A-Za-z0-9]{36,255}\b"),
        ),
        _SecretRule(
            category="aws-access-key",
            pattern=re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        ),
        # anthropic-key must run BEFORE openai-key.
        _SecretRule(
            category="anthropic-key",
            pattern=re.compile(r"\bsk-ant-[A-Za-z0-9_-]{32,}\b"),
        ),
        _SecretRule(
            category="openai-key",
            pattern=re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_-]{32,}"),
        ),
        _SecretRule(
            category="bearer",
            pattern=re.compile(r"\bBearer [A-Za-z0-9_\-\.=]{20,}"),
        ),
        _SecretRule(
            category="jwt",
            pattern=re.compile(
                r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
            ),
        ),
        _SecretRule(
            category="slack-token",
            pattern=re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
        ),
    )
    return block_rules, line_rules


_BLOCK_RULES, _LINE_RULES = _build_secret_rules()
_AWS_SECRET_LINE_HINT = re.compile(r"aws_secret_access_key", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScrubConfig:
    """Scrubber configuration.  Frozen for safe sharing across calls."""

    scrub_secrets: bool = True
    scrub_profanity: bool = True
    profanity_words: tuple[str, ...] = DEFAULT_PROFANITY


# ---------------------------------------------------------------------------
# Core scrubbing
# ---------------------------------------------------------------------------


def _build_profanity_re(words: tuple[str, ...]) -> Pattern[str] | None:
    """Compile a whole-word regex covering all profanity words.

    Returns ``None`` if the list is empty.
    """
    cleaned = sorted({w.strip().lower() for w in words if w.strip()})
    if not cleaned:
        return None
    body = "|".join(re.escape(w) for w in cleaned)
    return re.compile(rf"\b(?:{body})\b", re.IGNORECASE)


def _replacement_for(rule: _SecretRule) -> str:
    """Return the substitution string for a secret rule."""
    if rule.replace is not None:
        return rule.replace
    return f"[REDACTED:{rule.category}]"


def _scrub_block_secrets(text: str, counts: Counter[str]) -> str:
    """Apply whole-document redactions (PEM/GPG/env-secret)."""
    for rule in _BLOCK_RULES:
        new_text, n = rule.pattern.subn(
            _replacement_for(rule),
            text,
        )
        if n:
            counts[rule.category] += n
            text = new_text
    return text


def _scrub_line_secrets(line: str, counts: Counter[str]) -> str:
    """Apply per-line redactions, honoring rule order and context."""
    for rule in _LINE_RULES:
        if rule.category == "aws-secret-key" and not _AWS_SECRET_LINE_HINT.search(line):
            continue
        new_line, n = rule.pattern.subn(
            _replacement_for(rule),
            line,
        )
        if n:
            counts[rule.category] += n
            line = new_line
    return line


def scrub(text: str, config: ScrubConfig | None = None) -> tuple[str, dict[str, int]]:
    """Scrub *text* per *config*.  Return ``(scrubbed_text, redaction_counts)``.

    Counts only include categories that fired at least once.
    """
    if config is None:
        config = ScrubConfig()

    counts: Counter[str] = Counter()
    if not text:
        return text, dict(counts)

    if config.scrub_secrets:
        text = _scrub_block_secrets(text, counts)
        scrubbed_lines = [
            _scrub_line_secrets(line, counts) for line in text.splitlines(keepends=True)
        ]
        text = "".join(scrubbed_lines)

    if config.scrub_profanity:
        prof_re = _build_profanity_re(config.profanity_words)
        if prof_re is not None:
            new_text, n = prof_re.subn("[REDACTED:profanity]", text)
            if n:
                counts["profanity"] += n
                text = new_text

    return text, dict(counts)


# ---------------------------------------------------------------------------
# Integration helper
# ---------------------------------------------------------------------------

# Module-level default config — created once, shared across calls.
_DEFAULT_CONFIG = ScrubConfig()


def scrub_and_log(text: str, label: str) -> str:
    """Scrub *text* and log redaction counts at INFO level.

    *label* identifies the call site in the log message (e.g.
    ``"pre-compact"`` or ``"backfill"``).  Returns the scrubbed text.
    """
    scrubbed, counts = scrub(text, _DEFAULT_CONFIG)
    if counts:
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
        logger.info("%s: scrubbed capture file (%s)", label, summary)
    return scrubbed
