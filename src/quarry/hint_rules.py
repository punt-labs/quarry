"""Convention hint rules: instant and sequence-based.

Pure functions that map commands (and recent event history) to
advisory hint strings.  No I/O, no side effects, fully deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from quarry.hint_accumulator import ToolEvent

# ---------------------------------------------------------------------------
# Instant rules — fire on the current command alone
# ---------------------------------------------------------------------------


class _Predicate(Protocol):
    def __call__(self, command: str) -> bool: ...


@dataclass(frozen=True)
class _InstantRule:
    """An instant hint rule with pattern, hint text, and optional refinement."""

    id: str
    pattern: re.Pattern[str]
    hint: str
    refinement: _Predicate | None = None


def _is_uv_pip(command: str) -> bool:
    """Check if *command* is ``uv pip install`` (not bare ``pip``)."""
    return bool(re.search(r"(?:^|\s)uv\s+pip\s+install\b", command))


_STRIP_QUOTES = re.compile(r""""[^"]*"|'[^']*'""")

_NO_VERIFY_FLAG = re.compile(r"\s(-n\b|--no-verify)\b")

_GIT_COMMIT_SEGMENT = re.compile(r"git\s+commit\b.*")


def _has_no_verify_flag(command: str) -> bool:
    """Check for ``-n`` or ``--no-verify`` in the ``git commit`` segment only.

    Scopes the check to the portion of the command starting at ``git commit``,
    so flags like ``head -n 5`` in chained commands don't false-positive.
    Quoted strings are stripped before matching.
    """
    match = _GIT_COMMIT_SEGMENT.search(command)
    if not match:
        return False
    segment = _STRIP_QUOTES.sub("", match.group())
    return bool(_NO_VERIFY_FLAG.search(segment))


_INSTANT_RULES: list[_InstantRule] = [
    _InstantRule(
        id="git-add-broad",
        pattern=re.compile(r"git\s+add\s+(-A|\.)(?=\s|$)"),
        hint="Reminder: stage specific files by name rather than `git add -A` or "
        "`git add .` — avoids accidentally staging secrets or large binaries.",
    ),
    _InstantRule(
        id="pip-install",
        pattern=re.compile(r"(?<!\S)pip\s+install\b"),
        hint="Reminder: use `uv` for package management, not `pip`.",
        refinement=lambda command: not _is_uv_pip(command),
    ),
    _InstantRule(
        id="force-push",
        pattern=re.compile(r"git\s+push\s.*(-f\b|--force(?!-))"),
        hint="Reminder: force-push is destructive — confirm this is intentional.",
    ),
    _InstantRule(
        id="no-verify",
        pattern=re.compile(r"git\s+commit\b"),
        hint="Reminder: do not skip hooks (`--no-verify`) unless explicitly asked.",
        refinement=_has_no_verify_flag,
    ),
]


def check_instant_rules(command: str) -> str | None:
    """Return the first matching instant hint, or ``None``."""
    for rule in _INSTANT_RULES:
        if rule.pattern.search(command):
            if rule.refinement is not None and not rule.refinement(command):
                continue
            return rule.hint
    return None


# ---------------------------------------------------------------------------
# Sequence rules — require temporal context from the accumulator
# ---------------------------------------------------------------------------

_MAKE_CHECK_PATTERN = re.compile(r"\bmake\s+check\b")

_FULL_GATE = "Reminder: run `make check` before committing."

_SOLO_GATE_HINT = "Tip: prefer `make check` over running sub-targets individually."

_SOLO_GATE_TARGETS = re.compile(r"^make\s+(lint|type|test)(?:\s|$)")


def _is_solo_gate(command: str) -> bool:
    """True if *command* runs a single make sub-target (lint, type, test)."""
    return bool(_SOLO_GATE_TARGETS.match(command))


def _command_has_full_gate(command: str) -> bool:
    """Check if *command* is ``make check``."""
    return bool(_MAKE_CHECK_PATTERN.search(command))


def check_sequence_rules(events: list[ToolEvent], command: str) -> str | None:
    """Return the first matching sequence hint, or ``None``.

    Parameters
    ----------
    events:
        Recent events from the accumulator (already pruned).
    command:
        The current command about to be executed.
    """
    # Rule: git commit without preceding full gate
    if re.search(r"\bgit\s+commit\b", command):
        recent = events[-10:]
        if not any(_command_has_full_gate(e.command) for e in recent):
            return _FULL_GATE

    # Rule: 2+ consecutive solo gate tools
    if _is_solo_gate(command):
        consecutive = 0
        for e in reversed(events):
            if _is_solo_gate(e.command):
                consecutive += 1
            else:
                break
        if consecutive >= 1:
            return _SOLO_GATE_HINT

    return None
