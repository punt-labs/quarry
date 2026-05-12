"""Extract structured identifiers from session transcript text."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionArtifacts:
    """Immutable container of identifiers extracted from a transcript."""

    commit_shas: tuple[str, ...]
    pr_numbers: tuple[int, ...]
    branch_names: tuple[str, ...]
    bead_ids: tuple[str, ...]


# -- Commit SHAs: 7-12 hex chars in git context --------------------------

_SHA_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "commit abc1234" in git log output
    re.compile(r"\bcommit\s+([0-9a-f]{7,12})\b"),
    # "[branch abc1234]" in git output
    re.compile(r"\[[^\]]+\s+([0-9a-f]{7,12})\]"),
    # Short SHA at start of `git log --oneline` lines
    re.compile(r"^([0-9a-f]{7,12})\s", re.MULTILINE),
)

# -- PR numbers: #N in pull-request context -------------------------------

_PR_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "PR #123", "pr #123"
    re.compile(r"\bPR\s*#(\d{1,5})\b", re.IGNORECASE),
    # "pull request #123", "pull #123"
    re.compile(r"\bpull\s+(?:request\s+)?#(\d{1,5})\b", re.IGNORECASE),
    # "merged #123"
    re.compile(r"\bmerged\s+#(\d{1,5})\b", re.IGNORECASE),
    # "pr/123" or "pulls/123" in URLs or branch-like patterns
    re.compile(r"\b(?:pr|pulls?)/(\d{1,5})\b", re.IGNORECASE),
)

# -- Branch names: word/word patterns in git context ----------------------

_BRANCH_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "checkout -b feat/xxx"
    re.compile(r"checkout\s+-b\s+([\w.-]+/[\w./-]+)"),
    # "push origin feat/xxx" (with optional flags between push and origin)
    re.compile(r"push\s+(?:-[^\s]+\s+)*\w+\s+([\w.-]+/[\w./-]+)"),
    # "branch feat/xxx"
    re.compile(
        r"\bbranch\s+((?:feat|fix|chore|release|bugfix|hotfix|perf|refactor|docs|test)/[\w./-]+)"
    ),
    # "(origin/branch-name)" in git log decorations
    re.compile(
        r"\((?:origin/)?((?:feat|fix|chore|release|bugfix|hotfix|perf|refactor|docs|test)/[\w./-]+)\)"
    ),
)

# -- Bead IDs: word-alphanum{2,6} in bead context ------------------------

_BEAD_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "bd close quarry-vdh6", "bd update quarry-vdh6", etc.
    re.compile(r"\bbd\s+\w+\s+([a-z]+-[a-z0-9]{2,6})\b"),
    # "bead quarry-vdh6" or "beads quarry-vdh6"
    re.compile(r"\bbeads?\s+([a-z]+-[a-z0-9]{2,6})\b"),
    # "Closes quarry-vdh6"
    re.compile(r"\bCloses\s+([a-z]+-[a-z0-9]{2,6})\b"),
    # "beads-quarry-vdh6" compound form
    re.compile(r"\bbeads?-([a-z]+-[a-z0-9]{2,6})\b"),
)


def extract_artifacts(text: str) -> SessionArtifacts:
    """Scan transcript text for commit SHAs, PR numbers, branches, and bead IDs."""
    shas: list[str] = []
    for pat in _SHA_PATTERNS:
        shas.extend(pat.findall(text))

    prs: list[int] = []
    for pat in _PR_PATTERNS:
        prs.extend(int(m) for m in pat.findall(text))

    branches: list[str] = []
    for pat in _BRANCH_PATTERNS:
        branches.extend(pat.findall(text))

    beads: list[str] = []
    for pat in _BEAD_PATTERNS:
        beads.extend(pat.findall(text))

    # Deduplicate while preserving first-seen order.
    return SessionArtifacts(
        commit_shas=tuple(dict.fromkeys(shas)),
        pr_numbers=tuple(dict.fromkeys(prs)),
        branch_names=tuple(dict.fromkeys(branches)),
        bead_ids=tuple(dict.fromkeys(beads)),
    )


def format_artifacts_header(artifacts: SessionArtifacts) -> str:
    """Format extracted artifacts as a structured text header."""
    lines: list[str] = []
    if artifacts.commit_shas:
        lines.append(f"Commits: {', '.join(artifacts.commit_shas)}")
    if artifacts.pr_numbers:
        lines.append(f"PRs: {', '.join(f'#{n}' for n in artifacts.pr_numbers)}")
    if artifacts.branch_names:
        lines.append(f"Branches: {', '.join(artifacts.branch_names)}")
    if artifacts.bead_ids:
        lines.append(f"Beads: {', '.join(artifacts.bead_ids)}")
    if not lines:
        return ""
    return "## Session Artifacts\n" + "\n".join(lines)
