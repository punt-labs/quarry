"""Guards the Ethos handoff checks: a subagent stop must stay fast and bounded."""

from __future__ import annotations

import json
import subprocess
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


def _selects(check: dict[str, object], paths: list[str]) -> bool:
    """Mirror ethos selection: ``all`` runs on any path list, even an empty one."""
    files = check["files"]
    if files == "all":
        return True
    assert isinstance(files, list), check["name"]
    return any(
        PurePosixPath(path).full_match(str(glob)) for path in paths for glob in files
    )


def _selected(paths: list[str]) -> set[str]:
    return {str(c["name"]) for c in _handoff_checks() if _selects(c, paths)}


def test_handoff_check_names_are_fast_and_type() -> None:
    assert [c["name"] for c in _handoff_checks()] == ["handoff-fast", "handoff-type"]


def test_handoff_fast_runs_on_every_handoff() -> None:
    fast = next(c for c in _handoff_checks() if c["name"] == "handoff-fast")
    assert fast["files"] == "all"
    for paths in (
        [],
        ["README.md"],
        ["Makefile"],
        ["uv.lock"],
        [".markdownlint-cli2.jsonc"],
        ["src/quarry/client.py"],
    ):
        assert "handoff-fast" in _selected(paths), paths


def test_handoff_type_runs_for_type_inputs_only() -> None:
    for path in ("src/quarry/client.py", "pyproject.toml", "uv.lock", "Makefile"):
        assert "handoff-type" in _selected([path]), path
    assert "handoff-type" not in _selected(["README.md"])
    assert "handoff-type" not in _selected([])


def test_check_handoff_target_expands_to_unit_tests_and_lint() -> None:
    result = subprocess.run(
        ["make", "-n", "check-handoff"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "pytest tests/test_client.py" in result.stdout
    assert "markdownlint-cli2" in result.stdout
    assert "ruff check ." in result.stdout
    assert "mypy" not in result.stdout


def test_handoff_timeouts_are_bounded() -> None:
    for check in _handoff_checks():
        assert "timeout" in check, check["name"]
        assert _seconds(str(check["timeout"])) <= HANDOFF_CHECK_TIMEOUT_SECONDS


def test_handoff_snapshot_covers_exactly_the_handoff_checks() -> None:
    snapshot = json.loads((CHECKS_DIR / "snapshots/quarry-default.yaml").read_text())
    assert set(snapshot["checks"]) == {str(c["name"]) for c in _handoff_checks()}
