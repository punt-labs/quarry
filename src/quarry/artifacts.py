"""Extract structured identifiers from session transcript text."""

from __future__ import annotations

import re
from dataclasses import dataclass

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


@dataclass(frozen=True)
class SessionArtifacts:
    """Immutable container of identifiers extracted from a transcript."""

    commit_shas: tuple[str, ...]
    pr_numbers: tuple[int, ...]
    branch_names: tuple[str, ...]
    bead_ids: tuple[str, ...]

    @classmethod
    def from_text(cls, text: str) -> SessionArtifacts:
        """Scan transcript text for commit SHAs, PR numbers, branches, and bead IDs."""
        return cls(
            commit_shas=cls._scan_strings(text, _SHA_PATTERNS),
            pr_numbers=cls._scan_ints(text, _PR_PATTERNS),
            branch_names=cls._scan_strings(text, _BRANCH_PATTERNS),
            bead_ids=cls._scan_strings(text, _BEAD_PATTERNS),
        )

    @staticmethod
    def _scan_strings(
        text: str, patterns: tuple[re.Pattern[str], ...]
    ) -> tuple[str, ...]:
        """Collect unique string matches from a sequence of patterns."""
        hits: list[str] = []
        for pat in patterns:
            hits.extend(pat.findall(text))
        return tuple(dict.fromkeys(hits))

    @staticmethod
    def _scan_ints(text: str, patterns: tuple[re.Pattern[str], ...]) -> tuple[int, ...]:
        """Collect unique integer matches from a sequence of patterns."""
        hits: list[int] = []
        for pat in patterns:
            hits.extend(int(m) for m in pat.findall(text))
        return tuple(dict.fromkeys(hits))

    def format_header(self) -> str:
        """Format extracted artifacts as a structured text header."""
        lines: list[str] = []
        if self.commit_shas:
            lines.append(f"Commits: {', '.join(self.commit_shas)}")
        if self.pr_numbers:
            lines.append(f"PRs: {', '.join(f'#{n}' for n in self.pr_numbers)}")
        if self.branch_names:
            lines.append(f"Branches: {', '.join(self.branch_names)}")
        if self.bead_ids:
            lines.append(f"Beads: {', '.join(self.bead_ids)}")
        if not lines:
            return ""
        return "## Session Artifacts\n" + "\n".join(lines)

    def format_frontmatter(self, session_id: str, timestamp: str) -> str:
        """Format artifacts as YAML frontmatter for the capture file."""
        if not session_id:
            return ""
        lines = [
            "---",
            f"session_id: {session_id}",
            f'timestamp: "{timestamp}"',
        ]
        if self.commit_shas:
            lines.append("commits:")
            lines.extend(f"  - {sha}" for sha in self.commit_shas)
        if self.pr_numbers:
            lines.append("prs:")
            lines.extend(f"  - {n}" for n in self.pr_numbers)
        if self.branch_names:
            lines.append("branches:")
            lines.extend(f"  - {b}" for b in self.branch_names)
        if self.bead_ids:
            lines.append("beads:")
            lines.extend(f"  - {bid}" for bid in self.bead_ids)
        lines.append("---")
        return "\n".join(lines)


# -- Thin wrappers preserving the module-level API -----------------------

# Direct aliases -- parameter order matches the method signatures.
extract_artifacts = SessionArtifacts.from_text
format_artifacts_header = SessionArtifacts.format_header


def format_artifacts_frontmatter(
    session_id: str,
    timestamp: str,
    artifacts: SessionArtifacts,
) -> str:
    """Format artifacts as YAML frontmatter for the capture file."""
    return artifacts.format_frontmatter(session_id, timestamp)
