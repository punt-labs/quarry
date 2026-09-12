"""Tests for the memory-corpus doctor checks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from quarry.doctor_memory import MemoryDiagnostics


def _write_ethos_config(root: Path, agent: str) -> None:
    """Deposit an ethos config at ``root/.punt-labs/ethos/config.yaml``."""
    config_dir = root / ".punt-labs" / "ethos"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(f"agent: {agent}\n")


def _patch_rows(rows: list[dict[str, object]]) -> MagicMock:
    """Return a Database facade whose chunks table replays *rows*."""
    facade = MagicMock()
    facade.db.list_tables.return_value.tables = ["chunks"]
    table = MagicMock()
    (
        table.search.return_value.limit.return_value.select.return_value.to_list.return_value
    ) = rows
    facade.db.open_table.return_value = table
    return facade


class TestCorpus:
    def test_no_data_yet_when_db_missing(self, tmp_path: Path) -> None:
        result = MemoryDiagnostics.corpus(tmp_path / "missing" / "lancedb")
        assert result.passed is True
        assert result.required is False
        assert result.message == "no data yet"

    def test_no_chunks_when_table_missing(self, tmp_path: Path) -> None:
        db_path = tmp_path / "lancedb"
        db_path.mkdir()
        facade = MagicMock()
        facade.db.list_tables.return_value.tables = []
        with patch("quarry.db.facade.Database.connect", return_value=facade):
            result = MemoryDiagnostics.corpus(db_path)
        assert result.passed is True
        assert result.message == "no chunks yet"

    def test_counts_handles_types_and_collections(self, tmp_path: Path) -> None:
        db_path = tmp_path / "lancedb"
        db_path.mkdir()
        rows: list[dict[str, object]] = [
            {"agent_handle": "rmh", "memory_type": "fact", "collection": "memory-rmh"},
            {"agent_handle": "rmh", "memory_type": "fact", "collection": "memory-rmh"},
            {
                "agent_handle": "kpz",
                "memory_type": "procedure",
                "collection": "memory-kpz",
            },
            # Knowledge chunk (no handle) — excluded from handle/type tallies but
            # its collection still shows. ``memory_type`` is a decayable value so
            # a broken exclusion would inflate ``procedure`` to 2, failing below.
            {"agent_handle": "", "memory_type": "procedure", "collection": "docs"},
        ]
        with patch("quarry.db.facade.Database.connect", return_value=_patch_rows(rows)):
            result = MemoryDiagnostics.corpus(db_path)
        assert result.passed is True
        assert "memory: rmh=2 kpz=1" in result.message
        assert "types: fact=2 procedure=1" in result.message
        assert "memory-rmh=2" in result.message
        assert "docs=1" in result.message

    def test_empty_handle_row_never_inflates_type_count(self, tmp_path: Path) -> None:
        """Knowledge chunks (empty handle) never tally under ``types:``.

        Ingestion may tag a chunk with a decayable ``memory_type`` even when
        no agent owns it; the corpus check counts memory rows only, so an
        anonymous ``fact`` chunk must not appear in the type tally.
        """
        db_path = tmp_path / "lancedb"
        db_path.mkdir()
        rows: list[dict[str, object]] = [
            {"agent_handle": "", "memory_type": "fact", "collection": "docs"},
            {"agent_handle": None, "memory_type": "procedure", "collection": "docs"},
        ]
        with patch("quarry.db.facade.Database.connect", return_value=_patch_rows(rows)):
            result = MemoryDiagnostics.corpus(db_path)
        assert "types:" not in result.message

    def test_ignores_non_decayable_memory_types(self, tmp_path: Path) -> None:
        db_path = tmp_path / "lancedb"
        db_path.mkdir()
        rows: list[dict[str, object]] = [
            {"agent_handle": "rmh", "memory_type": "chatter", "collection": "c"},
        ]
        with patch("quarry.db.facade.Database.connect", return_value=_patch_rows(rows)):
            result = MemoryDiagnostics.corpus(db_path)
        # ``rmh`` still tallies (handle non-empty) but ``chatter`` is not in
        # ``_MEMORY_TYPES``, so it does not show under ``types:``.
        assert "memory: rmh=1" in result.message
        assert "types:" not in result.message

    def test_db_error_returns_failed_check_not_raise(self, tmp_path: Path) -> None:
        db_path = tmp_path / "lancedb"
        db_path.mkdir()
        with patch(
            "quarry.db.facade.Database.connect", side_effect=RuntimeError("boom")
        ):
            result = MemoryDiagnostics.corpus(db_path)
        assert result.passed is False
        assert "check failed" in result.message

    def test_lessons_counted_despite_empty_agent_handle(self, tmp_path: Path) -> None:
        """A quarry-learn lesson row (empty agent_handle by design) still counts.

        Without D9's handle-independent path, a lesson row is invisible to
        both the handle and type tallies -- this is the gap D9 closes.
        """
        db_path = tmp_path / "lancedb"
        db_path.mkdir()
        lesson_row: dict[str, object] = {
            "agent_handle": "",
            "memory_type": "lesson",
            "collection": "quarry-lessons",
        }
        rows: list[dict[str, object]] = [lesson_row, dict(lesson_row)]
        with patch("quarry.db.facade.Database.connect", return_value=_patch_rows(rows)):
            result = MemoryDiagnostics.corpus(db_path)
        assert "lessons=2" in result.message

    def test_lessons_segment_absent_when_no_lessons(self, tmp_path: Path) -> None:
        db_path = tmp_path / "lancedb"
        db_path.mkdir()
        rows: list[dict[str, object]] = [
            {"agent_handle": "rmh", "memory_type": "fact", "collection": "memory-rmh"},
        ]
        with patch("quarry.db.facade.Database.connect", return_value=_patch_rows(rows)):
            result = MemoryDiagnostics.corpus(db_path)
        assert "lessons=" not in result.message

    def test_lessons_count_unaffected_by_agent_handle_presence(
        self, tmp_path: Path
    ) -> None:
        """A lesson row with a (non-standard) handle still counts as a lesson."""
        db_path = tmp_path / "lancedb"
        db_path.mkdir()
        rows: list[dict[str, object]] = [
            {"agent_handle": "rmh", "memory_type": "lesson", "collection": "c"},
        ]
        with patch("quarry.db.facade.Database.connect", return_value=_patch_rows(rows)):
            result = MemoryDiagnostics.corpus(db_path)
        assert "lessons=1" in result.message


class TestIdentityActive:
    def test_no_handle_when_config_missing(self, tmp_path: Path) -> None:
        result = MemoryDiagnostics.identity_active(str(tmp_path), tmp_path / "lancedb")
        assert result.passed is True
        assert result.message == "no ethos identity active"

    def test_handle_active_but_db_missing_passes(self, tmp_path: Path) -> None:
        _write_ethos_config(tmp_path, "rmh")
        result = MemoryDiagnostics.identity_active(
            str(tmp_path), tmp_path / "missing" / "lancedb"
        )
        assert result.passed is True
        assert "rmh" in result.message

    def test_handle_active_and_db_empty_passes(self, tmp_path: Path) -> None:
        _write_ethos_config(tmp_path, "rmh")
        db_path = tmp_path / "lancedb"
        db_path.mkdir()
        with patch("quarry.db.facade.Database.connect", return_value=_patch_rows([])):
            result = MemoryDiagnostics.identity_active(str(tmp_path), db_path)
        assert result.passed is True

    def test_handle_active_with_rows_passes(self, tmp_path: Path) -> None:
        _write_ethos_config(tmp_path, "rmh")
        db_path = tmp_path / "lancedb"
        db_path.mkdir()
        with patch(
            "quarry.db.facade.Database.connect",
            return_value=_patch_rows([{"agent_handle": "rmh"}]),
        ):
            result = MemoryDiagnostics.identity_active(str(tmp_path), db_path)
        assert result.passed is True
        assert "rmh" in result.message
        assert "1 memory rows" in result.message

    def test_corpus_only_has_knowledge_chunks_passes(self, tmp_path: Path) -> None:
        """Empty-handle rows are corpus, not memory: no rows to compare against.

        A repo with only ingested docs (all handles NULL/empty) must pass
        cleanly — the "someone else's memory but not mine" warning is not
        triggered when nobody owns a memory row.
        """
        _write_ethos_config(tmp_path, "rmh")
        db_path = tmp_path / "lancedb"
        db_path.mkdir()
        rows: list[dict[str, object]] = [
            {"agent_handle": ""},
            {"agent_handle": None},
        ]
        with patch("quarry.db.facade.Database.connect", return_value=_patch_rows(rows)):
            result = MemoryDiagnostics.identity_active(str(tmp_path), db_path)
        assert result.passed is True
        assert "PreCompact" not in result.message

    def test_handle_active_but_no_matching_rows_warns(self, tmp_path: Path) -> None:
        """The core "PreCompact never fired" warning: rows exist, but not mine."""
        _write_ethos_config(tmp_path, "rmh")
        db_path = tmp_path / "lancedb"
        db_path.mkdir()
        # Corpus has rows, but none carry ``rmh``.
        rows: list[dict[str, object]] = [
            {"agent_handle": "kpz"},
            {"agent_handle": "kpz"},
        ]
        with patch("quarry.db.facade.Database.connect", return_value=_patch_rows(rows)):
            result = MemoryDiagnostics.identity_active(str(tmp_path), db_path)
        assert result.passed is False
        assert result.required is False
        assert "rmh" in result.message
        assert "PreCompact" in result.message

    def test_db_error_returns_failed_check_not_raise(self, tmp_path: Path) -> None:
        _write_ethos_config(tmp_path, "rmh")
        db_path = tmp_path / "lancedb"
        db_path.mkdir()
        with patch(
            "quarry.db.facade.Database.connect", side_effect=RuntimeError("boom")
        ):
            result = MemoryDiagnostics.identity_active(str(tmp_path), db_path)
        assert result.passed is False
        assert "check failed" in result.message
