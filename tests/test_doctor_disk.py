"""The disk-footprint and free-space doctor checks."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import patch

import lancedb

from quarry.db.table_probe import TableProbe
from quarry.doctor_disk import FootprintCheck, FreeDiskCheck

if TYPE_CHECKING:
    from pathlib import Path

_GIB = 1024**3


class _Usage(NamedTuple):
    total: int
    used: int
    free: int


def _table(db_path: Path, rows: int = 20) -> Path:
    db = lancedb.connect(str(db_path))
    row = {"id": 1, "text": "hello" * 200}
    db.create_table("chunks", [row] * rows)
    return db_path / "chunks.lance"


class TestFootprintCheck:
    def test_no_database_passes(self, tmp_path: Path) -> None:
        result = FootprintCheck.check(tmp_path / "missing")
        assert (result.passed, result.required) == (True, False)
        assert "no database" in result.message

    def test_no_table_passes(self, tmp_path: Path) -> None:
        tmp_path.mkdir(exist_ok=True)
        result = FootprintCheck.check(tmp_path)
        assert result.passed is True
        assert "no table" in result.message

    def test_normal_table_passes(self, tmp_path: Path) -> None:
        _table(tmp_path)
        assert FootprintCheck.check(tmp_path).passed is True

    def test_oversized_table_warns_without_failing_the_report(
        self, tmp_path: Path
    ) -> None:
        table_dir = _table(tmp_path)
        with (table_dir / "bloat").open("wb") as f:
            f.truncate(2 * _GIB)  # sparse: counted by size, no real disk used
        result = FootprintCheck.check(tmp_path)
        assert (result.passed, result.required) == (False, False)
        assert "quarry doctor" not in result.message
        assert "GiB" in result.message

    def test_unexpected_statistics_shape_is_a_failed_check(
        self, tmp_path: Path
    ) -> None:
        _table(tmp_path)
        with patch.object(TableProbe, "_live_bytes", side_effect=ValueError("shape")):
            result = FootprintCheck.check(tmp_path)
        assert result.passed is False
        assert "ValueError" in result.message

    def test_probe_failure_is_a_failed_check_not_a_crash(self, tmp_path: Path) -> None:
        _table(tmp_path)
        with patch.object(TableProbe, "sizes", side_effect=OSError("boom")):
            result = FootprintCheck.check(tmp_path)
        assert result.passed is False
        assert result.message


class TestFreeDiskCheck:
    def test_normal_free_space_passes(self, tmp_path: Path) -> None:
        with patch.object(
            shutil, "disk_usage", return_value=_Usage(100 * _GIB, 0, 50 * _GIB)
        ):
            result = FreeDiskCheck.check(tmp_path)
        assert (result.passed, result.required) == (True, False)

    def test_low_free_space_warns(self, tmp_path: Path) -> None:
        with patch.object(
            shutil, "disk_usage", return_value=_Usage(100 * _GIB, 0, _GIB)
        ):
            result = FreeDiskCheck.check(tmp_path)
        assert (result.passed, result.required) == (False, False)
        assert "GiB free" in result.message

    def test_missing_database_dir_measures_nearest_existing_parent(
        self, tmp_path: Path
    ) -> None:
        with patch.object(
            shutil, "disk_usage", return_value=_Usage(100 * _GIB, 0, 50 * _GIB)
        ):
            result = FreeDiskCheck.check(tmp_path / "not" / "yet")
        assert result.passed is True

    def test_probe_failure_is_a_failed_check_not_a_crash(self, tmp_path: Path) -> None:
        with patch.object(shutil, "disk_usage", side_effect=OSError("boom")):
            result = FreeDiskCheck.check(tmp_path)
        assert result.passed is False
        assert result.message
