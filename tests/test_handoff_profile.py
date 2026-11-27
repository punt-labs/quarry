"""Guards the Ethos handoff checks: a subagent stop must stay fast and bounded."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parent.parent
CHECKS_DIR = ROOT / ".punt-labs/ethos/checks"
HANDOFF_CHECK_TIMEOUT_SECONDS = 120


def _load_profile() -> dict[str, object]:
    loaded = yaml.safe_load((CHECKS_DIR / "quarry-default.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def _handoff_checks() -> list[dict[str, object]]:
    checks = _load_profile()["checks"]
    assert isinstance(checks, list)
    return [c for c in checks if c["stage"] == "handoff"]


def _seconds(duration: str) -> int:
    scale = {"s": 1, "m": 60}
    return int(duration[:-1]) * scale[duration[-1]]


def test_handoff_stage_does_not_run_the_full_gate() -> None:
    offenders = [
        c["name"]
        for c in _handoff_checks()
        if c.get("adapter") == "make" and c.get("target") == "check"
    ]
    assert offenders == []


def _matches(check: dict[str, object], path: str) -> bool:
    files = check["files"]
    assert isinstance(files, list), check["name"]
    return any(PurePosixPath(path).full_match(str(glob)) for glob in files)


def _matching(path: str) -> set[str]:
    return {str(c["name"]) for c in _handoff_checks() if _matches(c, path)}


def test_handoff_checks_list_their_files() -> None:
    assert _handoff_checks(), "the profile must keep a handoff check"
    for check in _handoff_checks():
        assert isinstance(check["files"], list), check["name"]


def test_docs_only_change_runs_lint_but_not_tests_or_types() -> None:
    matched = _matching("README.md")
    assert "handoff-lint" in matched
    assert "handoff-unit" not in matched
    assert "handoff-type" not in matched


def test_python_change_runs_every_handoff_check() -> None:
    assert _matching("src/quarry/client.py") == {
        "handoff-unit",
        "handoff-lint",
        "handoff-type",
    }


def test_lint_inputs_outside_python_trigger_lint() -> None:
    for path in (
        "plugin/commands/find.md",
        "plugin/hooks/hooks.json",
        "pyproject.toml",
    ):
        assert "handoff-lint" in _matching(path), path


def test_type_checker_settings_trigger_type() -> None:
    assert "handoff-type" in _matching("pyproject.toml")


def test_handoff_timeouts_are_bounded() -> None:
    for check in _handoff_checks():
        assert "timeout" in check, check["name"]
        assert _seconds(str(check["timeout"])) <= HANDOFF_CHECK_TIMEOUT_SECONDS


def test_handoff_snapshot_covers_exactly_the_handoff_checks() -> None:
    snapshot = json.loads((CHECKS_DIR / "snapshots/quarry-default.yaml").read_text())
    assert set(snapshot["checks"]) == {str(c["name"]) for c in _handoff_checks()}
