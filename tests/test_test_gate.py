"""Regression tests for test selection and quarantine metadata."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools.test_changed import select_tests
from tools.test_quarantine import pytest_collection_modifyitems


def test_changed_source_selects_direct_imports(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    selected = Path("tests/test_client.py")
    (tmp_path / selected).write_text("from quarry.client import Client\n")
    (tmp_path / "tests" / "test_other.py").write_text("import quarry.other\n")
    source = Path("src/quarry/client.py")
    assert select_tests(tmp_path, {source}) == [selected]
    assert select_tests(tmp_path, {source, Path("src/quarry/unmapped.py")}) is None
    assert select_tests(tmp_path, {Path("tests/conftest.py")}) is None
    assert select_tests(tmp_path, {Path("README.md")}) == []


def test_changed_scripts_select_their_contracts_or_the_default(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    script = Path("scripts/test-wheel.sh")
    assert select_tests(tmp_path, {script}) is None
    selected = Path("tests/test_build_scripts.py")
    (tmp_path / selected).touch()
    assert select_tests(tmp_path, {script}) == [selected]
    assert select_tests(tmp_path, {Path(".github/workflows/nightly.yml")}) is None


def test_changed_module_selects_tests_using_parent_reexports(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    direct = Path("tests/test_client.py")
    through_package = Path("tests/test_mcp.py")
    (tmp_path / direct).write_text("from quarry.client.client import QuarryClient\n")
    (tmp_path / through_package).write_text("from quarry.client import QuarryClient\n")
    source = Path("src/quarry/client/client.py")
    assert select_tests(tmp_path, {source}) == [direct, through_package]


@pytest.mark.parametrize(
    ("issue", "days", "valid"),
    [
        ("PL-123", 14, True),
        ("PL-123", 15, False),
        ("PL-123", -1, False),
        ("", 1, False),
    ],
)
def test_quarantine_collection_validates_metadata(
    request: pytest.FixtureRequest, issue: str, days: int, *, valid: bool
) -> None:
    """Check marker validation without adding a permanent quarantined item."""
    parent = request.node.parent
    assert parent is not None
    item = pytest.Function.from_parent(parent, name="quarantined", callobj=lambda: None)
    marker = pytest.mark.quarantine(
        issue=issue,
        owner="Test maintainer",
        expires=(datetime.now(UTC).date() + timedelta(days=days)).isoformat(),
    )
    item.add_marker(marker)
    if valid:
        pytest_collection_modifyitems([item])
    else:
        with pytest.raises(pytest.UsageError, match="quarantine requires"):
            pytest_collection_modifyitems([item])
