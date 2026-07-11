"""Secret-detection rule catalog for the scrubber.

Holds the regex rules that :class:`quarry.scrub.Scrubber` applies.  Kept
separate from the scrubbing engine so the rule data has one home and the
engine module stays focused on the passes themselves.

``BLOCK_RULES`` run once over the whole document (multi-line patterns);
``LINE_RULES`` run per-line so context-sensitive detectors can inspect
their own line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from re import Pattern

AWS_SECRET_LINE_HINT = re.compile(r"aws_secret_access_key", re.IGNORECASE)


@dataclass(frozen=True)
class SecretRule:
    """A single regex-based secret detector.

    Whether a rule runs whole-document or per-line is determined by which
    tuple it lives in — ``BLOCK_RULES`` or ``LINE_RULES``.
    """

    category: str
    pattern: Pattern[str]
    replace: str | None = None

    def replacement(self) -> str:
        """Return the substitution string for this rule."""
        if self.replace is not None:
            return self.replace
        return f"[REDACTED:{self.category}]"


BLOCK_RULES: tuple[SecretRule, ...] = (
    # PGP before PEM — PGP is technically PEM-shaped.
    SecretRule(
        category="gpg-private-key",
        pattern=re.compile(
            r"-----BEGIN PGP PRIVATE KEY BLOCK-----"
            r".*?"
            r"-----END PGP PRIVATE KEY BLOCK-----",
            re.DOTALL,
        ),
    ),
    SecretRule(
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
    SecretRule(
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


LINE_RULES: tuple[SecretRule, ...] = (
    # aws-secret-key: 40-char base64 on a line mentioning the key name.
    SecretRule(
        category="aws-secret-key",
        pattern=re.compile(
            r"(?<![A-Za-z0-9])(?!\[REDACTED)([A-Za-z0-9/+=]{40})(?![A-Za-z0-9])",
        ),
    ),
    SecretRule(
        category="gh-pat",
        pattern=re.compile(r"\b(?:ghp|ghs|ghu|gho|ghr)_[A-Za-z0-9]{36,255}\b"),
    ),
    SecretRule(
        category="aws-access-key",
        pattern=re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    # anthropic-key must run BEFORE openai-key.
    SecretRule(
        category="anthropic-key",
        pattern=re.compile(r"\bsk-ant-[A-Za-z0-9_-]{32,}\b"),
    ),
    SecretRule(
        category="openai-key",
        pattern=re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_-]{32,}"),
    ),
    SecretRule(
        category="bearer",
        pattern=re.compile(r"\bBearer [A-Za-z0-9_\-\.=]{20,}"),
    ),
    SecretRule(
        category="jwt",
        pattern=re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
    ),
    SecretRule(
        category="slack-token",
        pattern=re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
)
