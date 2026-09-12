"""Count lint/type suppressions in a Python codebase and enforce a ratchet."""

from __future__ import annotations

from .audit import SuppressionAudit, SuppressionAuditError
from .baseline import SuppressionBaseline, SuppressionBaselineError
from .cli import Cli, Options, main
from .gitio import GitError, GitRepo
from .outcome import Outcome
from .patterns import FileSuppressions
from .pyproject import PerFileIgnoresCounter
from .report import SuppressionReport
from .scanner import Scanner

__all__ = [
    "Cli",
    "FileSuppressions",
    "GitError",
    "GitRepo",
    "Options",
    "Outcome",
    "PerFileIgnoresCounter",
    "Scanner",
    "SuppressionAudit",
    "SuppressionAuditError",
    "SuppressionBaseline",
    "SuppressionBaselineError",
    "SuppressionReport",
    "main",
]
