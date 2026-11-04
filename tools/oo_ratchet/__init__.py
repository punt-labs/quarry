"""OO quality scorer and baseline ratchet — merge-base comparison in CI."""

from __future__ import annotations

from .apply import PlanApplier, UpdatePlan
from .audit import AuditError, AuditLog
from .baseline import Baseline, BaselineError
from .cli import Cli, Options, main
from .compare import FileReview, Review, Row
from .diffmodel import Diff, LineChange
from .gitio import GitError, GitRepo
from .metrics import ModuleMetrics
from .outcome import Outcome
from .provenance import SHAPE_METRICS, ExtractionDetector
from .ratchet import Ratchet
from .scorer import Scorer
from .thresholds import Thresholds
from .writer import BaselineWriter

__all__ = [
    "SHAPE_METRICS",
    "AuditError",
    "AuditLog",
    "Baseline",
    "BaselineError",
    "BaselineWriter",
    "Cli",
    "Diff",
    "ExtractionDetector",
    "FileReview",
    "GitError",
    "GitRepo",
    "LineChange",
    "ModuleMetrics",
    "Options",
    "Outcome",
    "PlanApplier",
    "Ratchet",
    "Review",
    "Row",
    "Scorer",
    "Thresholds",
    "UpdatePlan",
    "main",
]
